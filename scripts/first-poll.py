#!/usr/bin/env python3
"""scripts/first-poll.py — one poll cycle against YOUR settings.json (quickstart step 5).

Adoption dies in the gap between "the tests pass" and "it read a message from MY
config". This script closes that gap with one observable cycle: load config, build the
pipeline, poll every configured channel, store, classify, journal, persist the cursor.
Run it again and the journal count does not move — replay safety is something an adopter
should SEE, not take on faith.

First contact is read-only by construction: the send layer (core/outbox) is never
imported here, so no bug in this script can post as anyone. tests/test_docs.py enforces
that with an AST check on this file.

Exit codes: 0 = poll cycle completed; 2 = configuration refused (the loud-refusal
behaviour docs/RUNBOOK.md documents); anything else = a real bug, please file it.
"""
import argparse
import sys
from pathlib import Path

# Runs from a fresh clone with no install step, so the repo root (this file's
# grandparent) goes on sys.path explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.classify import Taxonomy, classify  # noqa: E402
from core.config import ConfigError, ensure_dirs, load, load_adapter_class  # noqa: E402
from core.journal import Journal  # noqa: E402
from core.store import Store  # noqa: E402


def demo_messages(inst):
    """One contract-shaped message per configured channel, for dry-run adapters."""
    return [{"channel_type": inst.adapter, "channel_id": ch.id,
             "sender_id": "U_DEMO", "sender_name": "quickstart-demo",
             "ts": f"{i}.0",
             "text": "Please review the quickstart demo message."}
            for i, ch in enumerate(inst.channels, start=1)]


def journal_message(journal, channel_id, msg, taxonomy):
    """Classify and journal one polled message. The journal row IS the proof the poll
    happened (mutation_check deletes this write; the docs test must go red)."""
    # attachments travel to the classifier (ENH-4): passing only msg["text"] here
    # would re-drop every upload however well the adapter and classifier handle them,
    # and an image-only message would journal as an empty STATEMENT again.
    c = classify(msg["text"], taxonomy, attachments=msg.get("attachments"))
    # matched travels with the decision (R22): the journal row is where a classification
    # gets disputed, and the taxonomy may have changed by then. ambiguous travels too
    # (ENH-9): a dropped signal here reads back as never-classified, and the hedge
    # count starts broken for every adopter.
    return journal.record(channel_id, msg["ts"], sender_id=msg.get("sender_id"),
                          text=msg.get("text"), kind=c.kind, reason=c.reason,
                          matched=c.matched, ambiguous=c.ambiguous)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Run one poll/classify/journal cycle from a settings file.")
    ap.add_argument("--config", default="settings.json",
                    help="path to your settings.json (default: ./settings.json)")
    ap.add_argument("--seed-demo", action="store_true",
                    help="plant one demo message per channel in adapters that expose a "
                         "dry-run seed() hook (the fake adapter does), so the first poll "
                         "has something to show")
    args = ap.parse_args(argv)

    try:
        cfg = load(args.config)
    except ConfigError as ex:
        print(f"REFUSED: {ex}", file=sys.stderr)
        return 2
    ensure_dirs(cfg)

    store = Store(cfg.store_path)
    journal = Journal(cfg.journal_path)
    polled = journaled = 0
    try:
        for inst in cfg.instances:
            adapter = load_adapter_class(cfg.channels_dir, inst.adapter)(auth=inst.auth)
            taxonomy = Taxonomy.from_config(inst.taxonomy)
            if args.seed_demo:
                if hasattr(adapter, "seed"):
                    adapter.seed(demo_messages(inst))
                else:
                    print(f"note: adapter {inst.adapter!r} has no dry-run seed() hook; "
                          "polling it as-is")
            for ch in inst.channels:
                cursor = store.cursor_get(inst.name, ch.id)
                messages, new_cursor = adapter.poll(cursor)
                # poll() is adapter-wide; the engine owns per-channel attribution. A
                # message for a channel this instance does not watch is not ours to keep.
                mine = [m for m in messages if m.get("channel_id") == ch.id]
                store.upsert_messages(mine)
                fresh = 0
                for m in mine:
                    if journal_message(journal, ch.id, m, taxonomy):
                        fresh += 1
                # The cursor is adapter-opaque (channels/CONTRACT.md): persist what the
                # adapter returned, never parse it. Re-polls may duplicate, never lose,
                # and the journal absorbs the duplicates.
                if new_cursor is not None and new_cursor != cursor:
                    store.cursor_set(inst.name, ch.id, new_cursor)
                print(f"{inst.name}/{ch.id}: polled {len(mine)} message(s), "
                      f"{fresh} new in the journal")
                polled += len(mine)
                journaled += fresh
    finally:
        store.close()
        journal.close()

    print(f"FIRST POLL OK — {polled} polled, {journaled} journaled; "
          f"state under {cfg.state_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
