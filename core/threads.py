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

Follow-ups (issue #8): an answered ask is no longer the open-ask sweep's, so a reply posted
under the answer later (typically the asker's next question) was never ingested, listed or
owed. `sweep_followups()` sweeps the threads of asks answered within FOLLOWUP_WINDOW_S, at
the lowest priority (the scheduler gives it what the open asks left of the budget), and ties
nothing. Not covered: a follow-up in a thread rooted at the answerer's own TOP-LEVEL message;
recognising that root needs the source's answerer list or the engine's own delivery metadata.
A second route to answered, at INGEST (threaded_is_answered, 2026-10-31): a history row of
a thread parent carries the platform's own summary of the thread under it, and the first
poll of a channel serves every parent that way. `answered_from_summary()` reads it through
the adapter's optional `thread_summary(message)` and ties an open ask whose summary names a
replier other than the asker — no platform call, so the sweep's window and a 429 no longer
hold a visibly answered ask open. Measured on the aws-do-agent integration (operator,
2026-10-31): asks whose thread anyone could see on the platform read UNANSWERED for as long
as the bounded sweep took to reach them, and the dashboard kept "qualifying them as
unanswered". The sweep stays the route for a thread that grows AFTER its parent was polled
while that ask is still open: a cursored history poll never serves a parent twice, and the
sweep works from open asks. Once an ask is tied, by either route, `sweep_followups()` fetches
its thread like any answered ask's for FOLLOWUP_WINDOW_S, so a question already in it, or
posted there later, is journaled and owed; when the source names `answerers`, only theirs
tie another person's ask here too.
"""
from __future__ import annotations

import json

from .classify import ACTIONABLE_KINDS
from .delivery import ENGINE_EVENT_TYPE
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

# Issue #8: how long after its answer an ask's thread is still swept for follow-ups. Bounded
# on purpose: sweeping every answered thread forever would grow without limit, and a day
# covers the asker who follows up the next morning.
FOLLOWUP_WINDOW_S = 24 * 3600


def answered_by(asker: str | None, replies, is_answer=None, answerers=()) -> dict | None:
    """The earliest reply from someone OTHER than the asker that IS an answer, or None.

    The asker bumping their own thread is not an answer; a reply with no sender, or the
    adapters' unattributed placeholder, cannot be credited to anyone and answers nothing;
    and a reply the caller classifies as an ask of its own ("can you also share the
    logs?") is a new question, not an answer — crediting it closed the original ask and
    its owed work (independent review F7). `is_answer` is the caller's verdict; the
    reference scheduler passes its classifier.

    `answerers` (ENH-31, operator decision 2026-10-13): when the source names who may
    close an ask — the agent's bot user, the operators — only THEIR replies close OTHER
    people's asks, so a bystander's "+1" closes nothing (review S8). An ask posed BY a
    listed answerer (the agent's clarifying question) is closed by any non-asker's
    answer — the other party — or it could never close and would owe the engine an
    answer to its own agent forever (review F10). Empty keeps the any-non-asker rule.
    The ask-shaped filter applies to answerers too: a listed answerer's clarifying
    question is a question, not an answer ("follow-up questions never credited").
    """
    restricted = bool(answerers) and asker not in answerers
    for r in sorted(replies, key=lambda m: ts_order(m["ts"])):
        sender = r.get("sender_id")
        if not sender or sender == asker or sender in UNATTRIBUTED_SENDERS:
            continue
        if _is_engine_post(r):
            # The engine's own delivery was tied to ITS ask by the surface when it was
            # proven; only that proof ties it. Crediting it here would close another ask
            # under the same root with one reply, or record a MISMATCH post — one the
            # surface refused to call delivered — as an answer (review round 2, R2-3).
            continue
        if restricted and sender not in answerers:
            continue
        if is_answer is not None and not is_answer(r):
            continue
        return r
    return None


def close_dm_asks(store, journal, channel: str, *, answerers=(), is_answer=None,
                  refusals=None) -> list:
    """The DM answered-state (ENH-31 B): in a 1:1 direct message every message is
    top-level (measured on the aws-do-agent integration: 535 DM messages, 0 in-thread),
    so the thread sweep can never close a DM ask. Here each later top-level message
    that is an answer — by an answerer when the source names them, else by anyone but
    the asker (the other DM party) — closes the MOST RECENT still-open ask that precedes
    it AND that it can answer (never its own sender's: an answerer's reply skips its own
    clarifying question, review F10), and only that one, ever (review F1): a reply
    addresses the question just asked; an older question stays visibly open for a human to
    judge. The engine's own deliveries, recognised by the metadata the poll stores, are
    never credited — only the surface's proof ties them. No platform call: the candidates
    are the polled store. A non-orderable id space is refused by name into `refusals`.
    Returns [(ask_ts, message_ts), ...] for the ties made.
    """
    open_asks = sorted(open_asks_for(journal, channel), key=lambda r: ts_order(r["ts"]))
    if not open_asks:
        return []
    if any(ts_order(a["ts"])[0] != 0 for a in open_asks):
        # A non-numeric id space (an email Message-ID) cannot be ordered; the sweep refuses
        # it by name and so does this rule — "later than the ask" would be a guess (N3).
        # NAMED to the caller (round 2, R2-13): the scheduler reports it, as it reports
        # the sweep's refusals, instead of an empty list that reads as "nothing to do".
        if refusals is not None:
            refusals.append((channel, "non-orderable id space — the DM rule cannot tell a "
                                      "later message from an earlier one; declare the "
                                      "channel kind 'channel', or use numeric ids"))
        return []
    arrivals = store.arrivals(channel)
    # One message closes at most ONE ask, ever: an answer already spent on an ask must
    # not close the next-older open ask on the next cycle (review F1).
    used = journal.used_response_keys(channel, "dm-reply:")
    answered = []
    for m in store.top_level_after(channel, open_asks[0]["ts"]):
        if f"dm-reply:{m['ts']}" in used:
            continue
        # (An engine delivery — tied to ITS ask by the surface when it was proven — is
        # refused by answered_by itself, which the target loop below goes through: review
        # F1 addendum, round 2 R2-3. One check, in the one place both rules share.)
        preceding = [a for a in open_asks if ts_order(a["ts"]) < ts_order(m["ts"])]
        # The most recent open ask this message can answer: an answerer's reply skips
        # its own clarifying question and reaches the human's ask behind it (review F10
        # — before, the bot's answer "answered" the bot's own question and was dropped).
        target = None
        for a in reversed(preceding):
            if answered_by(a["sender_id"], [m], is_answer, answerers) is not None:
                target = a
                break
        if target is None:
            continue
        journal.mark_responded(channel, target["ts"], f"dm-reply:{m['ts']}",
                               at=arrivals.get(str(m["ts"])))
        used.add(f"dm-reply:{m['ts']}")
        answered.append((str(target["ts"]), str(m["ts"])))
        open_asks.remove(target)
    return answered


def _is_engine_post(m) -> bool:
    """Does this stored message carry the engine's own delivery marker? Adapters that can
    carry metadata (Slack) attach ENGINE_EVENT_TYPE to every post; it rides the stored
    raw payload. Anything unparseable is not an engine post."""
    raw = m.get("raw")
    if not raw:
        return False
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return False
    if not isinstance(payload, dict):          # a raw that parses to 123, a list, true
        return False
    meta = payload.get("metadata")
    if not isinstance(meta, dict):             # "metadata": "x" is not the marker either
        return False
    return meta.get("event_type") == ENGINE_EVENT_TYPE
def answered_from_summary(adapter, journal, channel: str, messages, *, answerers=(),
                          errors=None) -> list | None:
    """Tie the open asks among `messages` that visibly have a thread — without a call.

    For each polled TOP-LEVEL message whose journal row is an open ask (ACTIONABLE_KINDS),
    the adapter's `thread_summary(message)` is asked what the row says about its thread:
    `{reply_count, repliers, latest_reply}`, or None. The rule is the sweep's, as far as
    a summary lets it be applied: the FIRST replier other than the asker answers (response
    key `thread-reply:summary:<replier>`, so the dashboard counts it among the thread
    answers); a thread whose only repliers are the asker stays open; the adapters'
    unattributed placeholder answers nothing; a count with no attributable replier stays
    open — a replier the platform did not name would be a guess, and the engine never
    guesses. The replies' text is not in view here, so the sweep's `is_answer` verdict
    cannot be applied; the setting that turns this route on turns that verdict off too
    (core/schedule.py: in the sweep a reply that is itself a question still answers, and
    is owed on its own row). This route fetches no thread, so a question already in a tied
    ask's thread is not journaled HERE; the follow-up sweep of answered asks' threads
    (`sweep_followups()`, issue #8) fetches it, in this cycle or a later one. The tie is
    stamped at the poll that sighted the summary (`at` default):
    the reply's own platform time predates the row's first sighting, and a tie stamped
    there would read every such ask as EDITED AFTER RESPONSE.

    `answerers` is answered_by()'s rule (ENH-31): when the source names them, only their
    replies tie another person's ask; an ask posed by a listed answerer is tied by any
    non-asker. An exception from `thread_summary()` on one row is recorded by ask into
    `errors` and the next row is still read: the caller commits its cursor after this, and
    an escaping exception would hold the whole channel at this window (never a crash loop).

    Returns [(ask_ts, replier), ...]; None when the adapter has no `thread_summary` —
    the capability is absent, which the caller must read as "cannot conclude", never as
    "no thread" (the same rule as sweep()).
    """
    if not hasattr(adapter, "thread_summary"):
        return None
    ties = []
    for m in messages:
        if m.get("thread_id"):
            continue                        # a reply says nothing about its own thread
        row = journal.get(channel, m["ts"])
        if row is None or row["responded_at"] is not None or row["kind"] not in ACTIONABLE_KINDS:
            continue
        try:
            summary = adapter.thread_summary(m)
        except Exception as ex:  # noqa: BLE001 — recorded by ask, never swallowed
            if errors is not None:
                errors.append((str(m["ts"]), f"{type(ex).__name__}: {ex}"))
            continue
        if not summary:
            continue
        asker = row["sender_id"]
        restricted = bool(answerers) and asker not in answerers
        repliers = [r for r in (summary.get("repliers") or ())
                    if r and r != asker and r not in UNATTRIBUTED_SENDERS
                    and not (restricted and r not in answerers)]
        if not repliers:
            continue
        journal.mark_responded(channel, str(m["ts"]), f"thread-reply:summary:{repliers[0]}")
        ties.append((str(m["ts"]), repliers[0]))
    return ties


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


def followup_window_key(instance: str) -> str:
    """The store key the follow-up window is kept under, beside the instance's open-ask
    windows. A '#' never appears in an instance name (core/config refuses it), so the key
    cannot collide with a real instance."""
    return f"{instance}#followups"


def recently_answered_for(journal, channel: str, since: float) -> list:
    """This channel's ASKS answered at or after `since` (on the engine clock responded_at
    is stamped with), most recently answered first: the follow-up sweep's list."""
    rows = [r for r in journal.answered(channel, since=since) if r["kind"] in ACTIONABLE_KINDS]
    rows.sort(key=lambda r: r["responded_at"], reverse=True)
    return rows


def sweep_followups(adapter, store, journal, channel: str, *, max_threads: int = SWEEP_MAX,
                    start: int = 0, now: float | None = None,
                    window_s: float = FOLLOWUP_WINDOW_S) -> dict | None:
    """Fetch new replies under the threads of asks answered within the last `window_s`
    seconds (issue #8). Once an ask is answered this is the only route by which a reply
    posted under it later reaches the engine.

    It never ties: an answered ask stays answered by what answered it, and a follow-up is
    never its answer (review F7). The caller journals the replies, so a follow-up QUESTION
    becomes an owed ask of its own and the open-ask sweep looks for ITS answer from the next
    cycle on. A root that has an open ask belongs to that sweep and is skipped here (one call
    per root per cycle). The fetch floor is the oldest answered ask under the root, as in
    sweep() (R9: the store and the journal absorb every re-sighting). `now` defaults to the
    store's clock, the one that stamps the arrivals a thread tie is dated by.

    Same result shape as sweep(); `answered` is always empty, and `next_start` rotates
    through the roots, most recently answered first. None when the adapter has no
    `replies()`: declared absent, never "nothing new".
    """
    if not hasattr(adapter, "replies"):
        return None
    now = store.clock() if now is None else now
    result = {"swept": 0, "calls": 0, "replies": [], "answered": [], "truncated": False,
              "rate_limited": None, "errors": [], "next_start": 0}
    open_roots = {store.thread_root(channel, r["ts"]) for r in open_asks_for(journal, channel)}
    by_root: dict = {}
    for row in recently_answered_for(journal, channel, now - window_s):
        if ts_order(row["ts"])[0] != 0:
            result["errors"].append((str(row["ts"]), "non-orderable message id — the "
                                     "thread sweep needs numeric platform timestamps"))
            continue
        root = store.thread_root(channel, row["ts"])
        if root in open_roots:
            continue
        by_root.setdefault(root, []).append(row)
    roots = list(by_root)
    if not roots:
        return result
    start = start % len(roots)
    turn = roots[start:] + roots[:start]
    window = turn[:max_threads]
    result["truncated"] = len(roots) > len(window)
    reached = len(window)
    for i, root in enumerate(window):
        rows = by_root[root]
        oldest = min((str(r["ts"]) for r in rows), key=ts_order)
        result["calls"] += 1            # an attempt is a call, whatever it answers
        try:
            fetched = list(adapter.replies(channel, root, oldest=oldest))
        except RateLimited as ex:
            result["rate_limited"] = ex.method or "replies"
            reached = i                 # this root was not fetched: resume at it
            break
        except Exception as ex:  # noqa: BLE001 — recorded by ask, never swallowed
            result["errors"].extend((str(r["ts"]), f"{type(ex).__name__}: {ex}")
                                    for r in rows)
            continue
        result["swept"] += 1
        if fetched:
            store.upsert_messages(fetched)
            result["replies"].extend(fetched)
    result["next_start"] = (start + reached) % len(roots)
    return result
