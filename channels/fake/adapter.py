"""channels/fake — the in-memory dry-run adapter (the reference implementation).

This is the adapter QUICKSTART points a new adopter at: it exercises the whole pipeline
(store -> classify -> journal -> outbox) with no network, no credentials, no platform.
It is also the copy-me example for adapter authors — every contract method, nothing else.

Two properties matter more than they look:

* `poll()` re-served from the same cursor returns the same messages — the contract says a
  re-poll may DUPLICATE but never lose (the live poller depends on gap-free overlapping
  windows, R9).
* `health()` can FAIL (set `fail_health`). Contract rule 5: a health check that can only
  pass is a defect — this repo exists partly because of one (docs/PROVENANCE.md).
"""


class Adapter:
    def __init__(self, auth=None):
        self.auth = auth or {}
        self.messages = []          # normalized messages, in ts order
        self.delivered = []         # (channel_id, text, key) — the audit trail
        self.fail_health = False

    def capabilities(self):
        return {"read": True, "history": True, "search": False,
                "send": True, "react": False, "threads": True}

    def seed(self, messages):
        """Test/dry-run hook: make messages available to the next poll()."""
        self.messages.extend(messages)

    def poll(self, cursor):
        """Gap-free and idempotent: same cursor -> same messages again, never fewer."""
        cursor_ts = float(cursor) if cursor is not None else float("-inf")
        fresh = [m for m in self.messages if float(m["ts"]) > cursor_ts]
        if not fresh:
            return [], cursor
        new_cursor = str(max(float(m["ts"]) for m in fresh))
        return fresh, new_cursor

    def resolve(self, ref):
        return ref

    def replies(self, channel_id, thread_ts, oldest=None):
        """Optional capability (channels/CONTRACT.md, ENH-30): every reply under one
        thread newer than `oldest` (exclusive), oldest-first, never the parent. The
        fake holds every message in one list, so this is a filter; a real adapter
        calls a second platform method because history never serves a reply."""
        floor = float(oldest) if oldest is not None else float("-inf")
        out = [m for m in self.messages
               if m.get("channel_id") == channel_id
               and m.get("thread_id") == thread_ts and float(m["ts"]) > floor]
        out.sort(key=lambda m: float(m["ts"]))
        return out

    def newest(self, channel_id, thread_id=None):
        """Optional capability (channels/CONTRACT.md, ENH-31): the newest message in the
        channel (top-level) or under one thread, or None — the outbox's pre-send
        re-read. Deliveries are not messages here (they live in `delivered`), so the
        engine's own posts never trip its own guard."""
        pool = [m for m in self.messages if m.get("channel_id") == channel_id
                and ((m.get("thread_id") == thread_id) if thread_id is not None
                     else not m.get("thread_id"))]
        return max(pool, key=lambda m: float(m["ts"])) if pool else None

    def send(self, channel_id, text, key=None, thread_id=None):
        """Placement is recorded, not ignored: an adapter that accepts thread_id and
        drops it posts in the main channel while reporting success, which is the one
        failure the thread scope exists to prevent (ENH-3)."""
        self.delivered.append((channel_id, text, key, thread_id))
        return {"ts": str(len(self.delivered)), "key": key}

    def read_back(self, target, key):
        """Proof-of-delivery by idempotency key — what outbox recovery relies on (R1)."""
        return any(t == target and k == key for t, _, k, _ in self.delivered)

    def health(self):
        if self.fail_health:
            return {"reachable": False, "auth_ok": False,
                    "detail": "failure injected via fail_health"}
        return {"reachable": True, "auth_ok": True, "detail": "in-memory, always up"}
