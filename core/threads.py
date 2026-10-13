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

Four refusals are load-bearing (each has a mutation in tests/mutation_check.sh):

* an adapter without `replies()` yields None — DECLARED absent, never "nothing new";
* the sweep is bounded per cycle (newest asks first) and says when it truncated, so a long
  backlog cannot turn one cycle into hundreds of platform calls;
* a platform 429 stops the sweep and is reported in the result, never swallowed;
* any other platform error on ONE thread (a deleted ask answers thread_not_found, forever)
  is recorded BY ASK and the sweep moves on — escaping would turn one dead thread into a
  scheduler crash loop that also leaves every later channel unpolled; swallowing it would
  be the F-2 silent no-op.

The fetch floor is always the ask itself (R9: the store and the journal absorb every
re-sighting). The first version used the newest STORED reply as the floor — but the store
is written inside the sweep and the journal only afterwards by the caller, so a crash between
the two left a stored reply that was never fetched again and never journaled: the inverted
dual-write the scheduler forbids for poll(), found by the independent review of 89fd0fa.
"""
from __future__ import annotations

from .classify import ACTIONABLE_KINDS
from .ratelimit import RateLimited
from .store import ts_order

# Threads swept per ADAPTER per cycle (the reference scheduler spends this one budget across
# all of a source's channels). Stated rather than unbounded: one open thread is one platform
# call, and Slack's history tier allows ~50 calls/minute — a backlog of a few hundred asks
# would make the sweep its own outage. The window ROTATES through the open asks (callers
# pass the `start` they got back as `next_start`), so an ask behind the newest few is
# reached as long as the window is wider than the arrival rate. It is a POSITION in the
# newest-first list: a one-ask window with a new ask every cycle re-fetches the same ask
# forever (independent review of #6, S1). A window keyed by ask ts is COMMS-79 item 18,
# due before any deployment sweeping 25+ channels on one adapter or at ~1 call per cycle.
SWEEP_MAX = 25

# Adapters map sender-less platform rows (system notices, deleted-message tombstones, some
# bots) to this placeholder — the store requires a sender. It is truthy, so it must be
# excluded by name: a tombstone is not an answer.
UNATTRIBUTED_SENDERS = frozenset({"unknown"})


def answered_by(asker: str | None, replies, is_answer=None, answerers=()) -> dict | None:
    """The earliest reply from someone OTHER than the asker that IS an answer, or None.

    The asker bumping their own thread is not an answer; a reply with no sender, or the
    adapters' unattributed placeholder, cannot be credited to anyone and answers nothing;
    and a reply the caller classifies as an ask of its own ("can you also share the
    logs?") is a new question, not an answer — crediting it closed the original ask and
    its owed work (independent review F7). `is_answer` is the caller's verdict; the
    reference scheduler passes its classifier.

    `answerers` (ENH-31, operator decision 2026-10-13): when the source names who may
    close an ask — the agent's bot user, the operators — only THEIR replies count, so a
    bystander's "+1" closes nothing (review S8). Empty keeps the any-non-asker rule. The
    ask-shaped filter applies to answerers too: a listed answerer's clarifying question
    is a question, not an answer ("follow-up questions never credited").
    """
    for r in sorted(replies, key=lambda m: ts_order(m["ts"])):
        sender = r.get("sender_id")
        if not sender or sender == asker or sender in UNATTRIBUTED_SENDERS:
            continue
        if answerers and sender not in answerers:
            continue
        if is_answer is not None and not is_answer(r):
            continue
        return r
    return None


def close_dm_asks(store, journal, channel: str, *, answerers=(), is_answer=None) -> list:
    """The DM answered-state (ENH-31 B): in a 1:1 direct message every message is
    top-level (measured on the aws-do-agent integration: 535 DM messages, 0 in-thread),
    so the thread sweep can never close a DM ask. Here each later top-level message
    that is an answer — by an answerer when the source names them, else by anyone but
    the asker (the other DM party) — closes the MOST RECENT still-open ask that precedes
    it, and only that one: a reply addresses the question just asked; an older question
    stays visibly open for a human to judge. No platform call: the candidates are the
    polled store. Returns [(ask_ts, message_ts), ...] for the ties made.
    """
    open_asks = sorted(open_asks_for(journal, channel), key=lambda r: ts_order(r["ts"]))
    if not open_asks:
        return []
    arrivals = store.arrivals(channel)
    answered = []
    for m in store.top_level_after(channel, open_asks[0]["ts"]):
        preceding = [a for a in open_asks if ts_order(a["ts"]) < ts_order(m["ts"])]
        if not preceding:
            continue
        target = preceding[-1]
        if answered_by(target["sender_id"], [m], is_answer, answerers) is None:
            continue
        journal.mark_responded(channel, target["ts"], f"dm-reply:{m['ts']}",
                               at=arrivals.get(str(m["ts"])))
        answered.append((str(target["ts"]), str(m["ts"])))
        open_asks.remove(target)
    return answered


def open_asks_for(journal, channel: str) -> list:
    """This channel's open ASKS (ACTIONABLE_KINDS only), newest first — the one list the
    sweep works from, exposed so the cycle can tell a channel the budget skipped that
    owes nothing from one left behind (review F4)."""
    rows = [r for r in journal.unanswered(channel) if r["kind"] in ACTIONABLE_KINDS]
    rows.sort(key=lambda r: ts_order(r["ts"]), reverse=True)
    return rows


def sweep(adapter, store, journal, channel: str, *,
          max_threads: int = SWEEP_MAX, start: int = 0, is_answer=None,
          answerers=()) -> dict | None:
    """Fetch new replies under this channel's open asks and tie answers to them.

    Returns None when the adapter has no `replies()` — the capability is absent and the
    caller must read that as "cannot conclude", never as "nothing new". Otherwise::

        swept         threads actually fetched this call (one call per thread ROOT,
                      however many open asks live under it)
        calls         platform calls ATTEMPTED this call: fetched + refused + the one
                      that answered 429 — the budget charges this, never `swept` (an
                      all-errors channel was charged nothing; review ENH-33/F2)
        next_start    the `start` for the next call — the window rotates through the
                      open asks (newest first); a POSITION, so it reaches every ask only
                      while the window is wider than the arrival rate (review #6, S1)
        replies       every reply fetched this call (re-sightings included — the
                      journal absorbs them) — journal them
        answered      [(ask_ts, reply_ts), ...] ties made this call
        truncated     True when open asks remained beyond max_threads
        rate_limited  the platform method that answered 429, or None
        errors        [(ask_ts, 'ExcType: message'), ...] threads the platform refused,
                      and asks whose ids are not orderable (the sweep refuses to guess)
    """
    if not hasattr(adapter, "replies"):
        return None
    open_asks = open_asks_for(journal, channel)
    result = {"swept": 0, "calls": 0, "replies": [], "answered": [], "truncated": False,
              "rate_limited": None, "errors": [], "next_start": 0}
    if not open_asks:
        return result
    # The window rotates: start where the last call stopped, wrap around, and hand the
    # caller the next start — newest-first with a fixed slice starved every ask beyond
    # the newest few (independent review F9).
    start = start % len(open_asks)
    rotated = open_asks[start:] + open_asks[:start]
    batch = rotated[:max_threads]
    result["truncated"] = len(open_asks) > len(batch)
    # The window advances only as far as the loop GETS. Computed up front, a 429 after
    # the first fetch left it past every ask the batch never reached: under a tier of
    # one call per cycle the same ask was fetched forever and its siblings' answers
    # never closed them (independent review of ENH-33, R2-1).
    pos = {str(r["ts"]): i for i, r in enumerate(batch)}
    reached = len(batch)
    # One platform call per thread ROOT, however many open asks live under it.
    by_root: dict = {}
    for row in batch:
        if ts_order(row["ts"])[0] != 0:
            # A non-numeric id space (an email Message-ID) orders lexicographically, not
            # chronologically: "later than the ask" would be a guess. Refuse by name.
            result["errors"].append((str(row["ts"]), "non-orderable message id — the "
                                     "thread sweep needs numeric platform timestamps"))
            continue
        by_root.setdefault(store.thread_root(channel, row["ts"]), []).append(row)
    for root, rows in by_root.items():
        # Exclusive floor: the OLDEST ask under this root, every cycle (R9). Never the
        # newest stored reply: the store is written here and the journal only later by
        # the caller, so a store-derived floor would skip a reply that is stored but
        # not journaled (the inverted dual-write).
        oldest = min((str(r["ts"]) for r in rows), key=ts_order)
        result["calls"] += 1            # an attempt is a call, whatever it answers
        try:
            fetched = list(adapter.replies(channel, root, oldest=oldest))
        except RateLimited as ex:
            result["rate_limited"] = ex.method or "replies"
            reached = min(pos[str(r["ts"])] for r in rows)    # this root was not fetched
            break
        except Exception as ex:  # noqa: BLE001 — recorded by ask, never swallowed
            result["errors"].extend((str(r["ts"]), f"{type(ex).__name__}: {ex}")
                                    for r in rows)
            continue
        result["swept"] += 1
        if fetched:
            store.upsert_messages(fetched)
            result["replies"].extend(fetched)
        in_thread = store.thread_replies(channel, root)
        arrivals = store.arrivals(channel)
        for row in rows:
            later = [m for m in in_thread if ts_order(m["ts"]) > ts_order(row["ts"])]
            hit = answered_by(row["sender_id"], later, is_answer, answerers)
            if hit is not None:
                # Stamped at the reply's first ARRIVAL (engine clock, comparable with
                # revision.recorded_at), never at sweep time: an edit sighted between
                # the reply and this sweep must read as edited AFTER the answer (R23).
                journal.mark_responded(channel, row["ts"], f"thread-reply:{hit['ts']}",
                                       at=arrivals.get(str(hit["ts"])))
                result["answered"].append((str(row["ts"]), str(hit["ts"])))
    result["next_start"] = (start + reached) % len(open_asks)
    return result
