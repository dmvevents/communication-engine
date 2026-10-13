"""core/threads.py — in-thread answers close open asks (ENH-30).

Measured on the aws-do-agent integration (operator meeting 2026-10-12): every item on the
dashboard read UNANSWERED although the agent had answered each one, in-thread, through its
own Slack poster. Two facts explained it, and this module is the engine-side half of the fix
for both:

* `journal.responded_at` was set only by `Journal.mark_responded()`, which no production
  path called — an ask could not become answered by any route, so "unanswered for all items"
  was a certainty, not a bug in any one deployment;
* the Slack adapter polled `conversations.history` only, which never serves a thread reply,
  so the answers were not even ingested — "some comms are not listed" was every reply.

The sweep: for each open ASK in a channel (core/classify.ACTIONABLE_KINDS — a STATEMENT
owes nobody an answer and is never swept), fetch the replies under its thread that the
store has not seen, store them, return them for journaling, and tie the ask to the FIRST
reply from someone other than the asker. "Responded" means exactly that — somebody else
replied in the thread — not "resolved"; the response key names the reply so the audit trail
can be followed to it. A top-level reply in the channel cannot be attributed to the ask it
answers and is left alone: the engine never guesses.

Three refusals are load-bearing (each has a mutation in tests/mutation_check.sh):

* an adapter without `replies()` yields None — DECLARED absent, never "nothing new";
* the sweep is bounded per cycle (newest asks first) and says when it truncated, so a long
  backlog cannot turn one cycle into hundreds of platform calls;
* a platform 429 stops the sweep and is reported in the result, never swallowed.
"""
from __future__ import annotations

from .classify import ACTIONABLE_KINDS
from .ratelimit import RateLimited
from .store import ts_order

# Threads swept per channel per cycle. Stated rather than unbounded: one open ask is one
# platform call, and Slack's history tier allows ~50 calls/minute — a backlog of a few
# hundred asks would make the sweep its own outage. Newest first: the asks most likely to
# be getting answers right now.
SWEEP_MAX = 25


def answered_by(asker: str | None, replies) -> dict | None:
    """The earliest reply from someone OTHER than the asker, or None.

    The asker bumping their own thread is not an answer; a reply with no sender cannot be
    credited to anyone and answers nothing.
    """
    for r in sorted(replies, key=lambda m: ts_order(m["ts"])):
        if r.get("sender_id") and r["sender_id"] != asker:
            return r
    return None


def sweep(adapter, store, journal, channel: str, *,
          max_threads: int = SWEEP_MAX) -> dict | None:
    """Fetch new replies under this channel's open asks and tie answers to them.

    Returns None when the adapter has no `replies()` — the capability is absent and the
    caller must read that as "cannot conclude", never as "nothing new". Otherwise::

        swept         threads actually fetched this call
        replies       the messages fetched (not yet seen by the store) — journal them
        answered      [(ask_ts, reply_ts), ...] ties made this call
        truncated     True when open asks remained beyond max_threads
        rate_limited  the platform method that answered 429, or None
    """
    if not hasattr(adapter, "replies"):
        return None
    open_asks = [r for r in journal.unanswered(channel) if r["kind"] in ACTIONABLE_KINDS]
    open_asks.sort(key=lambda r: ts_order(r["ts"]), reverse=True)
    batch = open_asks[:max_threads]
    result = {"swept": 0, "replies": [], "answered": [],
              "truncated": len(open_asks) > len(batch), "rate_limited": None}
    for row in batch:
        root = store.thread_root(channel, row["ts"])
        known = store.thread_replies(channel, root)
        # Exclusive floor: the newest reply already stored, else the ask itself. The
        # store is the cursor — no second cursor table to drift from it.
        oldest = max([str(row["ts"])] + [str(m["ts"]) for m in known], key=ts_order)
        try:
            fetched = list(adapter.replies(channel, root, oldest=oldest))
        except RateLimited as ex:
            result["rate_limited"] = ex.method or "replies"
            break
        result["swept"] += 1
        if fetched:
            store.upsert_messages(fetched)
            result["replies"].extend(fetched)
        later = [m for m in store.thread_replies(channel, root)
                 if ts_order(m["ts"]) > ts_order(row["ts"])]
        hit = answered_by(row["sender_id"], later)
        if hit is not None:
            journal.mark_responded(channel, row["ts"], f"thread-reply:{hit['ts']}")
            result["answered"].append((str(row["ts"]), str(hit["ts"])))
    return result
