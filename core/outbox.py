"""core/outbox.py — the ONLY path to a send (gate G2; requirements R1, R2, R10).

The incumbent system reconciled a send/commit race **24 times**: a responder sent a reply,
died before persisting its cursor, and the loop had to prove-a-reply-existed-in-Slack to
self-heal. That is the classic dual-write problem, and detection-after-the-fact is not a
fix. This module makes the sequence recoverable by construction.

The ladder, in order, all durable:

    INTENT     written BEFORE the adapter is called. If we crash here, recovery knows a
               send *may* have happened and must check.
    SENT       the adapter returned a receipt.
    VERIFIED   a read-back proved the message is actually on the target.
    COMMITTED  safe for the caller to advance its own cursor.

Recovery resumes from **INTENT**, never from a cursor: for every unfinished row it asks the
adapter "is a message carrying this idempotency key already on the target?" — the same
proof-of-delivery technique the live system uses — and only re-sends when the answer is no.
That yields at-most-once delivery with at-least-once *attempt*, i.e. exactly one message.

Reply policy is CONFIG, not code: `never` (default) / `staged` / `direct`. A `never` target
raises; a `staged` target writes a draft for an operator to gate and never calls the adapter.
The gate itself is `release()` (ENH-28): an explicit human action on the exact staged text,
which flips STAGED -> INTENT durably and then runs this same ladder; `discard()` is the
terminal rejection. No caller may reach `adapter.send()` except through `Outbox.send()` or
`Outbox.release()` — and both funnel through the one `_deliver()` seam.

Policy is resolvable PER SCOPE (ENH-3): a target's policy may be a plain string (both
scopes) or `{"channel": ..., "thread": ...}`, because "answer in thread, never the main
channel" is a real placement policy — and placement must survive a crash, so the row
records `scope` and `thread_id` and recovery re-sends into the recorded thread.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import re
import sqlite3
import time
from pathlib import Path

from core import redact
from core.delivery import DeliveryMismatch
from core.ratelimit import RateLimited

INTENT, SENT, VERIFIED, COMMITTED, STAGED = (
    "INTENT", "SENT", "VERIFIED", "COMMITTED", "STAGED")
# ENH-28: a draft a human looked at and rejected. Terminal, and KEPT — deleting the
# row would erase the record that this exact text reached the gate and was refused.
DISCARDED = "DISCARDED"
# ENH-31 guard 3: the platform shows a message carrying this row's key whose text is
# NOT the approved text. Terminal until a human acts: never re-sent (the message is
# there), never COMMITTED (it is not what was approved). recover() skips it.
MISMATCH = "MISMATCH"

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    key         TEXT PRIMARY KEY,
    target      TEXT NOT NULL,
    trigger_ts  TEXT NOT NULL,
    text        TEXT NOT NULL,
    state       TEXT NOT NULL,
    receipt     TEXT,
    policy      TEXT NOT NULL,
    scope       TEXT NOT NULL DEFAULT 'channel',
    thread_id   TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    seen_ts     TEXT
);
"""

# CREATE TABLE IF NOT EXISTS cannot add columns to an outbox.db that predates thread
# awareness, and an adopter's existing state must keep loading. The 'channel' backfill
# is truthful, not a guess: v1 had no way to express a thread, so every old row IS a
# channel-scope send.
_MIGRATIONS = (
    ("scope", "ALTER TABLE outbox ADD COLUMN scope TEXT NOT NULL DEFAULT 'channel'"),
    ("thread_id", "ALTER TABLE outbox ADD COLUMN thread_id TEXT"),
    # ENH-31 guard 2: the platform ts the human had seen when the draft was staged.
    ("seen_ts", "ALTER TABLE outbox ADD COLUMN seen_ts TEXT"),
)


def thread_block_reason(adapter) -> str | None:
    """Why this adapter cannot PLACE a reply in a thread, or None when it can. PUBLIC:
    the write surface decides a reply's placement with the same predicate the outbox
    refuses with, so the two can never disagree (review R2-4). Two
    sources, both consulted: the contract's declaration — an adapter whose
    capabilities()['threads'] is false is TOLD it may ignore thread_id (CONTRACT.md),
    so a **kwargs send() on such an adapter would flatten silently (independent review
    of ENH-34, F5) — and the signature, because a send() without the parameter cannot
    be handed one at all."""
    send = getattr(adapter, "send", None)
    if not callable(send):
        return "it has no send() at all"
    caps = getattr(adapter, "capabilities", None)
    if callable(caps) and not caps().get("threads"):
        return "it declares no thread support (capabilities()['threads'] is false)"
    if not _accepts_thread_id(send):
        return "its send() takes no thread_id (channels/CONTRACT.md)"
    return None


def _adapter_label(adapter) -> str:
    """The adapter by its channels/ directory name when it was loaded from one (every
    shipped adapter's class is named `Adapter`, which names nothing — review F10);
    the class name for anything else (test doubles)."""
    module = type(adapter).__module__ or ""
    prefix = "channel_adapter_"
    return module[len(prefix):] if module.startswith(prefix) else type(adapter).__name__


def _accepts_kw(fn, name: str) -> bool:
    """Does this callable take keyword `name` (named, or **kwargs)? Asked of the
    SIGNATURE, before the call — see _accepts_thread_id for why not try/except."""
    if not callable(fn):
        return False
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == name or p.kind is inspect.Parameter.VAR_KEYWORD for p in params)


def _allows_direct(adapter) -> bool:
    """May the engine post through this adapter without a human click? An adapter
    declares `capabilities()["direct"]` False when its authorization is a human-gated
    send only (Slack, 2026-10-13 grant); absent means allowed (the fake, test doubles)."""
    caps = getattr(adapter, "capabilities", None)
    if callable(caps):
        return bool(caps().get("direct", True))
    return True


def _accepts_thread_id(send) -> bool:
    """Can this adapter's send() be given a placement at all?

    Asked of the SIGNATURE, before the call: the alternative — call and interpret a
    TypeError — cannot tell "no such parameter" from a TypeError thrown inside a working
    adapter. **kwargs counts, since that is a conforming signature too.
    """
    try:
        params = inspect.signature(send).parameters
    except (TypeError, ValueError):
        # A builtin or C-implemented callable exposes no signature. Assume capable and
        # let the adapter itself refuse — guessing "incapable" would block a send the
        # policy allows.
        return True
    return ("thread_id" in params
            or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()))


class PolicyError(PermissionError):
    """The target's reply policy forbids sending. Never downgrade to a warning."""


class SendBlocked(RuntimeError):
    """The adapter refused or the read-back could not prove delivery."""


class ReleaseError(RuntimeError):
    """The row release()/discard() was pointed at cannot take that transition —
    the caller named a draft that does not exist, or one that is not a draft."""


def idempotency_key(target: str, trigger_ts: str, text: str,
                    thread_id: str | None = None) -> str:
    """Stable identity of a reply: who, what triggered it, exact content, placement.

    Deliberately content-inclusive — the live system's 41 byte-identical outcome
    signatures came from re-emitting the SAME text for the same trigger, which this key
    collapses into one delivery. Placement (thread vs channel) is part of the identity
    too: the same text in-thread and top-level are two different visible messages.
    The thread component is hashed only when present so channel-scope keys stay
    byte-identical to v1 keys — recovery must still match rows written before this.
    """
    h = hashlib.sha256()
    h.update(target.encode()); h.update(b"\x00")
    h.update(str(trigger_ts).encode()); h.update(b"\x00")
    h.update(text.encode())
    if thread_id is not None:
        h.update(b"\x00"); h.update(str(thread_id).encode())
    return h.hexdigest()


class Outbox:
    def __init__(self, db_path: str | Path, adapter, policies: dict[str, str] | None = None,
                 *, send_interval: float = 1.0,
                 clock=time.monotonic, sleep=time.sleep, marker: str | None = None):
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(outbox)")}
        for col, ddl in _MIGRATIONS:
            if col not in have:
                self.conn.execute(ddl)
        self.conn.commit()
        self.adapter = adapter
        # Default DENY. A target absent from the policy map cannot be sent to.
        self.policies = policies or {}
        # ENH-31 guard 1: the prefix every message must start with (config send_marker).
        # Several senders share one bot identity in the harness; the marker is how a
        # reader — and the other senders' dedupe — tells the engine's messages apart.
        self.marker = marker or None
        # ENH-31: an adapter that declares `direct` OFF (capabilities()["direct"] is
        # False — Slack's 2026-10-13 grant is a human click only) refuses a direct
        # policy HERE, at construction, loudly: a policy the adapter forbids must not sit
        # inert until the first send.
        if not _allows_direct(adapter):
            for target, pol in self.policies.items():
                scopes = pol.values() if isinstance(pol, dict) else (pol,)
                if "direct" in scopes:
                    raise PolicyError(
                        f"reply policy for {target} is 'direct', but adapter "
                        f"{_adapter_label(adapter)} declares direct sends off "
                        "(capabilities()['direct'] is false): only 'staged' — a human "
                        "click — is authorized for it")
        # ENH-13: chat.postMessage allows ~1 message/second PER CHANNEL
        # (docs.slack.dev/apis/web-api/rate-limits), so 1.0 is the default floor —
        # a burst of replies would trade this cheap local wait for a platform 429,
        # the exact seam the INTENT ladder exists to survive. Cheaper to not trip it.
        # `clock`/`sleep` are injectable like ratelimit.Backoff's, and for the same
        # reason: the tests must assert spacing to the float.
        self.send_interval = send_interval
        self._clock = clock
        self._sleep = sleep
        self._pace_last: dict[str, float] = {}

    def _pace(self, target: str) -> None:
        """Hold this attempt until >= send_interval after the last attempt to `target`.

        Keyed per channel because the platform scopes the limit to the channel — a
        global hold would let one busy channel silence every other one (the disease
        ENH-1 killed for methods). The wait is EXACTLY the remainder of the interval,
        never padded: padding is a locally-invented limit.

        The clock marks the ATTEMPT, not the success: a 429'd attempt consumed the
        channel's budget too, so an unspaced retry would re-trip the very 429 it is
        recovering from. In-memory on purpose, like ratelimit.Backoff — the horizon
        is ~1s, so a restarted process at worst sends one message slightly early per
        channel, and the platform's own 429 (surfaced, never swallowed) is the
        backstop.
        """
        last = self._pace_last.get(target)
        if last is not None:
            wait = last + self.send_interval - self._clock()
            if wait > 0:
                self._sleep(wait)
        self._pace_last[target] = self._clock()

    # ---- internals --------------------------------------------------------
    def _write(self, key, **cols):
        cols["updated_at"] = time.time()
        sets = ", ".join(f"{k}=?" for k in cols)
        self.conn.execute(f"UPDATE outbox SET {sets} WHERE key=?",
                          (*cols.values(), key))
        self.conn.commit()          # durable before we do anything observable

    def get(self, key):
        return self.conn.execute("SELECT * FROM outbox WHERE key=?", (key,)).fetchone()

    def _claim(self, key, from_state, to_state) -> bool:
        """Compare-and-swap on state — the one durable write that IS the click.

        release() used to read STAGED and then UPDATE unconditionally, so two Sends
        landing between the read and the write both reached the adapter: one approved
        draft, two messages (independent merge review 2026-10-13, repro R1), and a
        Discard interleaved with a Send still sent. With the state in the WHERE clause
        exactly one caller wins; the loser sees rowcount 0 and reads what the row
        became."""
        cur = self.conn.execute(
            "UPDATE outbox SET state=?, updated_at=? WHERE key=? AND state=?",
            (to_state, time.time(), key, from_state))
        self.conn.commit()
        return cur.rowcount == 1

    def _non_staged(self, row):
        """What a release() of a row that is NOT a draft reports — used before the
        claim (the sequential cases) and again after a lost claim (the concurrent
        ones), so every state has exactly one answer. None means: still STAGED."""
        if row["state"] in (VERIFIED, COMMITTED):
            # Already proven on the target: return the receipt, never re-send.
            return {"key": row["key"], "receipt": row["receipt"], "state": row["state"],
                    "deduped": True}
        if row["state"] in (INTENT, SENT):
            # A claim in flight, or one that died mid-flight. Only recover()'s
            # read-back can tell 'sent, unrecorded' from 'never sent' — a second
            # live delivery here is the double-message bug.
            return {"key": row["key"], "receipt": None, "state": row["state"],
                    "in_flight": True}
        if row["state"] == DISCARDED:
            raise ReleaseError(
                f"draft {row['key']!r} was DISCARDED — a rejected draft never sends; "
                "stage it again if it should")
        return None

    def _check_placement(self, thread_id, exc=SendBlocked, tail=""):
        """Refuse a thread reply this adapter could only fail to place — BEFORE a row
        is written or claimed. _deliver() refuses it too, but by then release() had
        flipped the row to INTENT: Discard refuses an INTENT row, and recover() re-ran
        the same refusal on every restart (independent delta review 2026-10-13, D1 —
        B3's class on the thread dimension). Top-level is NOT offered instead:
        flattening is the one outcome a thread-scoped policy forbids. An adapter with
        no send() at all is _can_send's business, not this check's."""
        send = getattr(self.adapter, "send", None)
        if thread_id is None or not callable(send):
            return
        blocked = thread_block_reason(self.adapter)
        if blocked is None:
            return
        raise exc(
            f"adapter {_adapter_label(self.adapter)} cannot place a reply in thread "
            f"{thread_id}: {blocked} — the outbox never moves a reply out of its thread "
            "on its own; a top-level answer is a different draft, allowed only where "
            f"the channel policy allows one{tail}")

    def _check_payload(self, text, exc=SendBlocked):
        """ENH-31 guards 1, 4, 5 — on the TEXT, before any row exists (stage, direct
        send) and again before the flip (release): the configured marker prefix, the
        adapter's size limit, and the BLOCK-tier credential scan. Each names its reason
        and never echoes a secret."""
        if self.marker and not re.match(re.escape(self.marker) + r"(\s|$)", text):
            raise exc(
                f"the message does not start with the configured marker {self.marker!r} "
                "— every send from this engine carries it so a reader, and the other "
                "senders' dedupe, can tell it apart; nothing sent")
        limit = getattr(self.adapter, "MAX_TEXT_CHARS", None)
        if limit is not None and len(text) > limit:
            raise exc(
                f"{len(text)} characters exceeds this adapter's {limit}-character limit "
                "— the platform would split it silently and return one ts; shorten it, "
                "nothing sent")
        hits = redact.scan(text)
        if hits:
            raise exc(
                f"the message matches the BLOCK-tier credential pattern(s) {hits} — "
                "refused before it could leave the host; nothing sent")

    def _seen_ts(self, target, thread_id):
        """The platform ts of the newest message a human staging now has seen: the newest
        message of the channel or the thread, or — for a thread with no replies yet — the
        thread's root, which IS what the human saw (review F4: without a platform baseline
        the click fell back to the host clock, and the first reply to arrive was compared
        against the wrong clock). None when the adapter has no newest(), or a channel is
        empty."""
        newest = getattr(self.adapter, "newest", None)
        if not callable(newest):
            return None
        latest = newest(target, thread_id)
        if latest is None:
            return None if thread_id is None else str(thread_id)
        return str(latest.get("ts"))

    def _check_newest(self, row):
        """ENH-31 guard 2 — at the CLICK: re-read the channel (or the thread) and refuse
        when anything the human has not seen has arrived. The baseline is the platform ts
        recorded when the draft was staged (`seen_ts`; re-staging refreshes it), so the
        comparison is platform clock against platform clock — the host clock never
        enters it (review F4). An adapter's newest() excludes this engine's own posts, so
        an earlier approved send does not refuse the next one. Rows without a baseline
        (staged before this column) fall back to the draft's created_at. Names who and
        when, never the text (a flash renders markdown)."""
        newest = getattr(self.adapter, "newest", None)
        if not callable(newest):
            return
        latest = newest(row["target"], row["thread_id"])
        if latest is None:
            return
        baseline = row["seen_ts"] if "seen_ts" in row.keys() else None
        try:
            latest_ts = float(latest.get("ts"))
            base = float(baseline) if baseline is not None else float(row["created_at"])
        except (TypeError, ValueError):
            return                     # a non-numeric id space cannot be compared
        if latest_ts > base:
            raise ReleaseError(
                f"re-read first: {latest.get('sender_id') or 'someone'} posted at ts "
                f"{latest.get('ts')} after this draft was staged, so the conversation "
                "moved on — the draft stays STAGED; re-read it, then stage the same text "
                "again to confirm, or discard")

    def _read_back(self, target, key, thread_id, text=None, since=None):
        """read_back(target, key), plus thread_id and the approved text for an adapter
        whose read_back takes them (Slack: a thread reply is served by
        conversations.replies, never history; the text lets a NEW process compare
        exactly, so a mangled post is a mismatch after a restart too — review F3).
        May raise core.delivery.DeliveryMismatch; callers park the row."""
        rb = self.adapter.read_back
        kwargs = {}
        if thread_id is not None and _accepts_kw(rb, "thread_id"):
            kwargs["thread_id"] = thread_id
        if text is not None and _accepts_kw(rb, "text"):
            kwargs["text"] = text
        if since is not None and _accepts_kw(rb, "since"):
            kwargs["since"] = since                  # bounds a restart search (review N8)
        return rb(target, key, **kwargs)

    def _deliver(self, target, text, key, thread_id):
        """The one place adapter.send() is called, from both the live path and recovery.

        `thread_id` is passed ONLY for a thread send: an adapter written before thread
        awareness (contract's `send(channel_id, text, thread_id?)`) has no such
        parameter, and a top-level reply through it must keep working untouched. A
        thread send through such an adapter must fail loudly rather than silently
        flatten into the main channel — that flattening is the exact behavioural
        difference this scope exists to control.
        """
        if thread_id is None:
            return self.adapter.send(target, text, key=key)
        blocked = thread_block_reason(self.adapter)
        if blocked is not None:
            raise SendBlocked(
                f"adapter {_adapter_label(self.adapter)} cannot place a reply in thread "
                f"{thread_id}: {blocked} — the outbox never moves a reply out of its "
                "thread on its own, so nothing was posted")
        # Deliberately NOT wrapped in `except TypeError`: a TypeError raised inside a
        # thread-capable adapter is that adapter's bug, and relabelling it "cannot
        # thread" would send an operator to fix a capability that was never missing.
        return self.adapter.send(target, text, key=key, thread_id=thread_id)

    def policy_for(self, target: str, scope: str = "channel") -> str:
        """Resolve the policy for a target AND placement.

        A plain string covers both scopes (every config written before ENH-3 keeps
        meaning what it meant). A dict scopes it, and a scope missing from that dict is
        DENIED — default-deny has to hold per scope, or "answer in thread, never the
        main channel" would silently become "answer anywhere".
        """
        policy = self.policies.get(target, "never")
        if isinstance(policy, dict):
            return policy.get(scope) or "never"      # a None value is deny, not direct
        return policy

    # ---- the send ladder --------------------------------------------------
    def send(self, target: str, trigger_ts: str, text: str,
             thread_id: str | None = None, _crash_at: str | None = None):
        """Deliver exactly once, or stage, or refuse. Returns a receipt dict.

        `thread_id` places the reply IN a thread; None means the main channel. The
        placement is policed (per-scope policy), keyed (two distinct messages) and
        recorded, so a crash cannot resume a thread reply as a top-level post.

        `_crash_at` is used ONLY by the fault-injection harness to simulate the process
        dying at a named seam; production callers never pass it.
        """
        scope = "thread" if thread_id is not None else "channel"
        policy = self.policy_for(target, scope)
        if policy == "never":
            raise PolicyError(
                f"reply policy for {target} ({scope} scope) is 'never' — refusing to "
                "send. Sending requires an explicit policy of 'staged' or 'direct'.")
        # Default DENY at the seam, not only at construction (review F5): anything that
        # is not exactly 'staged' or 'direct' is never — a typo is not a direct send —
        # and 'direct' sends only through an adapter that allows it. The policy map may
        # have changed since construction; the send is what must not happen.
        if policy not in ("staged", "direct"):
            raise PolicyError(
                f"reply policy for {target} ({scope} scope) is {policy!r}, not one of "
                "'never', 'staged', 'direct' — default deny, nothing sent")
        if policy == "direct" and not _allows_direct(self.adapter):
            raise PolicyError(
                f"reply policy for {target} ({scope} scope) is 'direct', but adapter "
                f"{_adapter_label(self.adapter)} declares direct sends off — only a human "
                "click ('staged') may send through it; nothing sent")

        key = idempotency_key(target, trigger_ts, text, thread_id)
        row = self.get(key)

        # Review D1: a thread reply this adapter could only fail to place is refused
        # BEFORE the INSERT that is the claim — an INTENT row for it would wedge. Only
        # when a NEW row would be minted (review F9): a replay of a reply already
        # delivered is answered from its row, whatever the adapter has since become.
        if row is None:
            self._check_placement(thread_id)        # before the INSERT that is the claim
            self._check_payload(text)               # marker, size, redaction (ENH-31)
            # This INSERT is also the CLAIM on the ladder below: concurrent senders of
            # the same reply race the SELECT above, and the primary key must arbitrate
            # to exactly one owner. OR IGNORE because the loser's insert is a clean
            # no-op, not an IntegrityError crash (6-sender probe delivered 3× before
            # this, fire=13).
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO outbox (key, target, trigger_ts, text, state, "
                "policy, scope, thread_id, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (key, target, str(trigger_ts), text,
                 STAGED if policy == "staged" else INTENT,
                 policy, scope, thread_id, time.time(), time.time()))
            self.conn.commit()      # R1: INTENT is durable BEFORE the adapter is touched
            if cur.rowcount == 0:   # lost the SELECT→INSERT race; read what the winner wrote
                row = self.get(key)

        if row is not None:
            # R2: already delivered (or proven) — return the existing receipt, do NOT re-send.
            if row["state"] in (VERIFIED, COMMITTED):
                return {"key": key, "receipt": row["receipt"], "state": row["state"],
                        "deduped": True}
            if row["state"] == STAGED:
                return {"key": key, "receipt": None, "state": STAGED, "deduped": True}
            # INTENT/SENT: another sender holds the claim. Sending here is the
            # double-delivery bug — and if the claimant DIED mid-flight, only
            # read-back can tell "sent, unrecorded" from "never sent", so the row
            # belongs to recover(), never to a second live send.
            return {"key": key, "receipt": None, "state": row["state"],
                    "in_flight": True}

        if policy == "staged":
            # Draft only. The operator gates the actual send; the adapter is never called.
            return {"key": key, "receipt": None, "state": STAGED, "staged": True}

        return self._finish(key, target, text, thread_id, _crash_at)

    def _finish(self, key, target, text, thread_id, _crash_at=None):
        """INTENT -> COMMITTED: the back half of the ladder, shared by the live send
        and an operator release (ENH-28). Extracted so release() runs the IDENTICAL
        pacing / delivery / durable-SENT / read-back / COMMITTED sequence a direct
        send runs — a parallel ladder would drift, and the seams here are exactly
        where drift double-messages someone. The caller must already hold the claim:
        an INTENT row it inserted (send) or one it flipped from STAGED (release)."""
        if _crash_at == "after_intent":
            raise _Crash("crash after INTENT, before adapter.send")

        self._pace(target)
        receipt = self._deliver(target, text, key, thread_id)

        if _crash_at == "after_send":
            # The nastiest seam: the message IS on the target but we never recorded it.
            raise _Crash("crash after adapter.send, before recording SENT")

        self._write(key, state=SENT, receipt=json.dumps(receipt))

        if _crash_at == "before_readback":
            raise _Crash("crash after SENT, before read-back")

        try:
            proven = self._read_back(target, key, thread_id, text)
        except DeliveryMismatch as ex:
            # The message IS on the platform, with text that is not the approved text.
            # Never re-send (it is there), never commit (it is not what was approved):
            # park it for a human (review F3).
            self._write(key, state=MISMATCH)
            raise SendBlocked(
                f"delivered with DIFFERENT text — {ex}; the row is parked as {MISMATCH}, "
                "nothing more will be sent, a human must resolve it") from ex
        if not proven:
            raise SendBlocked(f"read-back could not prove delivery of {key} to {target}")
        self._write(key, state=VERIFIED)

        if _crash_at == "before_commit":
            raise _Crash("crash after VERIFIED, before COMMITTED")

        self._write(key, state=COMMITTED)
        return {"key": key, "receipt": receipt, "state": COMMITTED, "deduped": False}

    # ---- recovery ---------------------------------------------------------
    def recover(self) -> dict:
        """Resume every unfinished row from INTENT. Returns a counts summary.

        For each row not yet VERIFIED/COMMITTED/STAGED, ask the target whether a message
        carrying this idempotency key already landed:
          * yes -> mark VERIFIED then COMMITTED. **No re-send.**
          * no  -> the send never happened; do it now.
        """
        counts = {"resumed": 0, "resent": 0, "already_delivered": 0, "mismatched": 0,
                  "errors": []}
        rows = self.conn.execute(
            "SELECT * FROM outbox WHERE state IN (?,?)", (INTENT, SENT)).fetchall()
        for row in rows:
            counts["resumed"] += 1
            key, target = row["key"], row["target"]
            # Per ROW (review F7): one row whose thread is gone, or whose post was
            # mangled, must not wedge every other approved send behind it. A 429 is the
            # platform saying stop, so it still ends the run.
            try:
                if self._read_back(target, key, row["thread_id"], row["text"],
                                   since=row["created_at"]):
                    counts["already_delivered"] += 1
                else:
                    # recovery is a burst source too: N undelivered rows for one channel
                    # re-sent back-to-back would 429 exactly like the live path
                    self._pace(target)
                    # Placement comes from the ROW, never from a caller: resuming a
                    # thread reply as a top-level post would break the thread-only
                    # policy after the fact, in public, with no second chance.
                    receipt = self._deliver(target, row["text"], key, row["thread_id"])
                    self._write(key, receipt=json.dumps(receipt))
                    counts["resent"] += 1
                    if not self._read_back(target, key, row["thread_id"], row["text"]):
                        raise SendBlocked(f"re-send of {key} still not provable")
                self._write(key, state=VERIFIED)
                self._write(key, state=COMMITTED)
            except RateLimited:
                raise
            except DeliveryMismatch as ex:
                self._write(key, state=MISMATCH)
                counts["mismatched"] += 1
                counts["errors"].append((key, f"{type(ex).__name__}: {ex}"))
            except Exception as ex:  # noqa: BLE001 — recorded by row, never a wedge
                counts["errors"].append((key, f"{type(ex).__name__}: {ex}"))
        return counts

    def pending(self) -> list:
        return self.conn.execute(
            "SELECT * FROM outbox WHERE state IN (?,?)", (INTENT, SENT)).fetchall()

    def staged(self) -> list:
        """Drafts at the operator gate, as plain dicts. This method exists for a HUMAN
        (the RUNBOOK sends one here to gate the drafts), and a raw sqlite3.Row prints
        as '<sqlite3.Row object at 0x...>' — the text being gated was reachable only
        via dict(row)['text'] and a source dive (ENH-22)."""
        rows = self.conn.execute(
            "SELECT * FROM outbox WHERE state=?", (STAGED,)).fetchall()
        return [dict(r) for r in rows]

    def delivered(self) -> list:
        """Rows PROVEN on the target (VERIFIED/COMMITTED), newest first, as plain
        dicts (the ENH-22 lesson, same as staged()). This is the 'sent' half of the
        operator surface's staged-vs-sent distinction (ENH-28): a draft is a promise,
        a row here is a read-back-proven fact, and the two must never render alike."""
        rows = self.conn.execute(
            "SELECT * FROM outbox WHERE state IN (?,?) ORDER BY updated_at DESC",
            (VERIFIED, COMMITTED)).fetchall()
        return [dict(r) for r in rows]

    # ---- the operator write path (ENH-28) -----------------------------------
    def stage(self, target: str, trigger_ts: str, text: str,
              thread_id: str | None = None) -> dict:
        """Write a draft at the operator gate. The adapter is NEVER called here.

        This is the compose path of the write surface: whatever the target's policy
        allows, a message composed by a human stops at STAGED, and only release() —
        an explicit second human action on the exact staged text — can move it
        further. Two properties are deliberate:

        * **Default deny holds for composing too.** A 'never' target refuses the
          draft outright — staging to it would imply someone could later approve a
          send the policy forbids.
        * **'direct' does not skip the gate.** A direct policy authorizes the ENGINE
          to answer without a human; a human composing on the operator surface is
          asking for a reviewed send, and the review is the point.
        """
        scope = "thread" if thread_id is not None else "channel"
        if self.policy_for(target, scope) == "never":
            raise PolicyError(
                f"reply policy for {target} ({scope} scope) is 'never' — refusing "
                "to stage. Composing requires a policy of 'staged' or 'direct'.")
        key = idempotency_key(target, trigger_ts, text, thread_id)
        if self.get(key) is None:               # a new draft, not a replay (review F9)
            self._check_placement(thread_id)        # before the draft exists at all
            self._check_payload(text)               # marker, size, redaction (ENH-31)
        # ENH-31 guard 2: what the human had SEEN when staging — the platform's newest
        # message, on the platform's clock — so the click compares platform ts with
        # platform ts (review F4: comparing with the host's created_at missed every
        # message inside the clock skew, and the engine's own earlier post tripped it).
        seen_ts = self._seen_ts(target, thread_id)
        # Same INSERT-is-the-claim shape as send(): concurrent stagers of the same
        # draft collapse to one row, and re-staging an already-delivered reply must
        # report the delivery, not mint a second visible message.
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO outbox (key, target, trigger_ts, text, state, "
            "policy, scope, thread_id, created_at, updated_at, seen_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (key, target, str(trigger_ts), text, STAGED,
             self.policy_for(target, scope), scope, thread_id,
             time.time(), time.time(), seen_ts))
        self.conn.commit()
        if cur.rowcount == 0:
            row = self.get(key)
            if row["state"] == STAGED:
                # Re-staging the SAME draft is the human saying "I re-read; keep this
                # text": the draft becomes current again (review F4: without this a
                # refused Reply had no exit — re-staging deduped onto the stale row).
                self.conn.execute("UPDATE outbox SET seen_ts=?, updated_at=? WHERE key=? "
                                  "AND state=?", (seen_ts, time.time(), key, STAGED))
                self.conn.commit()
                return {"key": key, "receipt": None, "state": STAGED, "deduped": True,
                        "re_read": True}
            return {"key": key, "receipt": row["receipt"], "state": row["state"],
                    "deduped": True}
        return {"key": key, "receipt": None, "state": STAGED, "staged": True,
                "deduped": False}

    def release(self, key: str, _crash_at: str | None = None) -> dict:
        """The human click — deliver ONE staged draft through the full ladder.

        The STAGED -> INTENT flip is written durably BEFORE the adapter is touched:
        that write IS the record of the approval, so a process death one instruction
        later is resumed by recover() and the approved send completes, instead of
        silently reverting to a draft nobody remembers approving. Everything after
        the flip is _finish() — the same code path a direct send runs.

        `_crash_at` is the fault-injection hook, as on send(); production callers
        never pass it.
        """
        row = self.get(key)
        if row is None:
            raise ReleaseError(
                f"no outbox row {key!r} — release() sends an explicitly staged "
                "draft and never mints one; stage() first")
        settled = self._non_staged(row)
        if settled is not None:
            return settled
        # Refuse BEFORE the flip when the adapter cannot deliver: flipping first and
        # failing inside the adapter wedged the row in INTENT — undiscardable, and
        # unrecoverable because recover() has the same adapter (review B3).
        if not _can_send(self.adapter):
            raise ReleaseError(
                f"the adapter for {row['target']} declares no send capability — the "
                "draft stays STAGED; it can only be discarded")
        # ...and when it cannot place THIS draft: a thread reply through a send() that
        # takes no thread_id used to fail inside _deliver(), after the flip (review D1).
        self._check_placement(row["thread_id"], ReleaseError,
                              " — the draft stays STAGED; it can only be discarded")
        # ENH-31: the text guards again (a marker configured after staging, a draft
        # written by older code), then the pre-send re-read — all before the flip.
        self._check_payload(row["text"], ReleaseError)
        self._check_newest(row)
        # Policy is re-resolved at the CLICK, not inherited from staging time: a
        # channel demoted to 'never' since the draft was written must refuse the
        # send, however long the draft sat at the gate.
        if self.policy_for(row["target"], row["scope"]) == "never":
            raise PolicyError(
                f"reply policy for {row['target']} ({row['scope']} scope) is now "
                "'never' — the draft stays staged; nothing was sent")
        if not self._claim(key, STAGED, INTENT):
            # Lost the race to another click (or a discard): report what the row
            # became — never a second delivery.
            settled = self._non_staged(self.get(key))
            if settled is None:
                raise ReleaseError(f"draft {key!r} changed under this click; retry")
            return settled
        try:
            return self._finish(key, row["target"], row["text"], row["thread_id"],
                                _crash_at)
        except RateLimited:
            # A 429 is answered BEFORE the platform posts, so nothing is on the platform
            # and the approval is not lost: hand the draft back to the gate (compare-and-
            # swap, INTENT -> STAGED) for the human to click again — a routine 429 used
            # to leave an approved reply with no Send and no Discard (review N4).
            self._claim(key, INTENT, STAGED)
            raise

    def discard(self, key: str) -> dict:
        """Reject a draft. Terminal, and only FROM staged: an in-flight or delivered
        row is not a draft, and 'discarding' one would misrecord what has already
        reached the platform. The row is kept (see DISCARDED above)."""
        row = self.get(key)
        if row is None:
            raise ReleaseError(f"no outbox row {key!r} to discard")
        # The same compare-and-swap as release(): a Discard that lands while a Send
        # is claiming must lose, never relabel a delivered row.
        if not self._claim(key, STAGED, DISCARDED):
            raise ReleaseError(
                f"only a STAGED draft can be discarded; {key!r} is "
                f"{self.get(key)['state']}")
        return {"key": key, "receipt": None, "state": DISCARDED}

    def close(self):
        self.conn.close()


def _can_send(adapter) -> bool:
    """Does this adapter declare a send path? capabilities() is the contract's answer;
    an adapter without capabilities() (test doubles) is judged by having send()."""
    caps = getattr(adapter, "capabilities", None)
    if callable(caps):
        return bool(caps().get("send"))
    return callable(getattr(adapter, "send", None))


class _Crash(BaseException):
    """Simulated process death. Inherits BaseException so ordinary `except Exception`
    handlers cannot accidentally swallow it and hide a broken seam."""
