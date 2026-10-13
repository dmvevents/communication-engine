"""channels/slack — the Slack adapter (ENH-18 read path; ENH-31 send behind the outbox).

First REAL platform adapter, landed as a pure dir-drop (R11: zero core/ changes). Its
read path was authorized first (2026-08-26, this host's token: read-only, unchanged);
its send path was authorized on 2026-10-13 for the aws-do-agent harness only — the
harness's bot token, its SLACK_CHANNELS, policy `staged` (a human click), never
`direct`. The authorization is enforced in layers, not promised in prose:

* `send()` and `read_back()` exist, but `send()` is reachable only through core/outbox
  (channels/CONTRACT.md): the outbox's ladder (durable INTENT -> send -> read-back ->
  COMMITTED) and its pre-flip guards (marker prefix, newest-message re-read, size,
  redaction scan) stand between any caller and chat.postMessage;
* `_api`, the one funnel every request passes through, default-DENIES: any Web API
  method outside READ_METHODS plus the single WRITE method is refused before a byte
  reaches the wire. Without this, "one write method" would be one convenience wrapper
  away from becoming several;
* `capabilities()["direct"]` is False: an outbox built for a `direct` policy on this
  adapter refuses at construction, so the engine can never post without a human.

Polling follows channels/CONTRACT.md: gap-free (pagination followed to the end, or
fail loudly — a silent truncation is a lost message), idempotent from the same cursor,
and the cursor is adapter-private. Here it is a JSON object mapping channel id -> the
newest ingested ts, because `poll(cursor)` is adapter-wide while Slack history is
per-channel; a single scalar offset would let one busy channel's cursor skip another
channel's unread window.

The channel set and token both arrive as `env:NAME` references via config
(`auth: {"token": ..., "channels": ...}`, comma-separated ids). Channel ids ride the
ENVIRONMENT rather than a file on purpose: real Slack ids are host-specific literals
this repo's sanitize gate refuses in committed files, and the incumbent's env contract
(SLACK_CHANNELS) proved the pattern in production.

Rate limits (contract rule 3): a platform 429 is raised as core.ratelimit.RateLimited
carrying the platform's EXACT Retry-After and the method it hit — the engine's back-off
is keyed (instance, method) (ENH-1), so an unlabelled 429 would collapse that scope to
global and a read 429 would silence unrelated methods. The adapter never sleeps; it
only reports active holds through health().detail so an operator can see WHY polling
went quiet.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from core.ratelimit import RateLimited

API_BASE = "https://slack.com/api"
API_BASE_ENV = "COMMS_SLACK_API_BASE"
PAGE_LIMIT = 200          # Slack's recommended per-page maximum for history/list reads


def api_base(env=None) -> str:
    """The Web API base: Slack's, unless COMMS_SLACK_API_BASE names a LOOPBACK stub.

    The override exists so the whole poll / sweep / send / read-back path can be
    exercised against a local stub in tests (ENH-31 C) — never to point a live adapter
    at a third host. So anything but http://127.0.0.1:<port> or http://localhost:<port>
    (no path, query, or credentials) is refused by name at construction, before any
    I/O: a workspace token must not be sendable to an address an env var chose.
    """
    value = (os.environ if env is None else env).get(API_BASE_ENV, "")
    if not value:
        return API_BASE
    u = urllib.parse.urlparse(value)
    if (u.scheme != "http" or u.hostname not in ("127.0.0.1", "localhost")
            or not u.port or u.path not in ("", "/") or u.query or u.fragment
            or u.username is not None):
        raise ValueError(
            f"{API_BASE_ENV} must be http://127.0.0.1:<port> or http://localhost:<port> — "
            f"a loopback stub for tests, never a third host; refusing {value!r}")
    return f"http://{u.hostname}:{u.port}"

# Deny-by-default: everything this adapter is allowed to call, exhaustively. Extending
# the adapter means extending this list IN THE SAME DIFF as the test that uses it.
READ_METHODS = frozenset({
    "auth.test",
    "conversations.history",
    "conversations.info",
    "conversations.list",
    "conversations.replies",
    "users.info",
    "users.list",
})
# The ONE write method, and the only caller of it is send() (ENH-31). chat.update,
# chat.delete, reactions.*, files.* stay refused: the grant is "post a human-approved
# reply", nothing else.
WRITE_METHODS = frozenset({"chat.postMessage"})
MAX_TEXT_CHARS = 3500     # Slack silently splits >4,000 chars and returns one ts (2026-09-18)
METADATA_EVENT = "comms_engine_reply"

# Platform id shapes (uppercase-only, so lowercase channel names can never collide —
# Slack forbids uppercase in channel names). Written so the pattern SOURCE cannot
# itself look like a real id to a secret scanner.
_CHANNEL_ID = re.compile(r"[CDG][A-Z0-9]{6,}")
_USER_ID = re.compile(r"[UW][A-Z0-9]{6,}")


class ApiError(RuntimeError):
    """Slack answered but the call failed (ok:false, or an unusable response body).
    Carries method + error code and never the token."""

    def __init__(self, method: str, error: str):
        self.method = method
        self.error = error
        super().__init__(f"{method} failed: {error}")


class ReadOnlyViolation(RuntimeError):
    """A method outside the allowlist reached the transport. The allowlist is the
    read methods plus chat.postMessage (authorized 2026-10-13, harness only, behind the
    outbox); the correct fix is never to widen it quietly — every method here was
    authorized by name."""

    def __init__(self, method: str):
        super().__init__(
            f"{method} is not on this adapter's allowlist (reads + chat.postMessage) — "
            f"refused before any I/O happens")


def _canonical(text: str) -> str:
    """Slack stores `&`, `<`, `>` escaped and may wrap a URL as `<url>` or `<url|label>`;
    compare both sides through this so "exact text" means exact CONTENT (ENH-31 guard 3)."""
    t = (text or "").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    t = re.sub(r"<(https?://[^|>]+)(?:\|[^>]*)?>", r"\1", t)
    return " ".join(t.split())


def _metadata_key(m) -> str | None:
    meta = m.get("metadata") or {}
    if meta.get("event_type") != METADATA_EVENT:
        return None
    return (meta.get("event_payload") or {}).get("key")


def _header(headers: dict, name: str, default=None):
    """HTTP header lookup, case-insensitive — proxies re-case them freely."""
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v
    return default


def _attachments(m):
    """Uploads ride the platform's `files` array, and the live system downloads those
    screenshots and treats them as content (ENH-4) — dropping them here turns an
    image-only message into an empty row for every downstream consumer. The message-
    level `attachments` array is deliberately NOT ingested: those are unfurls and bot
    decoration rendering links the text already carries, not new content. kind degrades
    to "file" for anything that is not an image — inventing categories from mimetype
    prefixes would call a PDF an "application"."""
    out = []
    for f in m.get("files") or ():
        mimetype = f.get("mimetype") or None
        out.append({
            "kind": "image" if (mimetype or "").startswith("image/") else "file",
            "name": f.get("name") or f.get("title"),
            "mimetype": mimetype,
            "url": f.get("url_private"),
        })
    return out


class Adapter:
    MAX_TEXT_CHARS = MAX_TEXT_CHARS      # read by core/outbox's size guard (ENH-31 guard 4)

    def __init__(self, auth=None, http=None, clock=None, env=None):
        auth = auth or {}
        # Resolved at construction so a refused override never reaches _api (ENH-31 C).
        self._api_base = api_base(env)
        self._token = auth.get("token")
        if not self._token:
            # Same load-time refusal rule as core/config.py: half-configured is the
            # worst state to discover during an incident.
            raise ValueError("slack adapter: auth['token'] is missing — configure it "
                             "as an env:NAME reference in settings.json")
        raw_channels = auth.get("channels") or ""
        self.channels = tuple(c.strip() for c in raw_channels.split(",") if c.strip())
        if not self.channels:
            raise ValueError("slack adapter: auth['channels'] is missing or empty — a "
                             "comma-separated channel-id list (env:NAME reference), "
                             "or poll() would silently watch nothing")
        self.http = http or self._urllib_http
        self._clock = clock or time.monotonic
        self._holds = {}   # method -> monotonic instant its 429 hold expires (report-only)
        self._sent = {}    # key -> {"ts", "channel", "text", "thread_id"} this process posted

    # ---- contract surface --------------------------------------------------
    def capabilities(self):
        # send: through core/outbox only, at policy `staged` — `direct` is declared
        # OFF, so an outbox built for a direct policy on this adapter refuses at
        # construction (the 2026-10-13 grant: human click only). threads: a reply is
        # placed with thread_ts, and conversations.replies serves the thread back.
        return {"read": True, "history": True, "search": False,
                "send": True, "direct": False, "react": False, "threads": True}

    # ---- the send path (ENH-31; only core/outbox may call these) ------------------
    def send(self, channel_id, text, key=None, thread_id=None):
        """chat.postMessage, once, with the outbox key in the message METADATA — the
        one place Slack lets a poster attach an idempotency key that read_back() can
        find again without altering the human-approved text. `thread_id` places the
        reply in that thread; None posts top-level. No guard lives here: the outbox
        refuses before the INTENT flip, and this method is the delivery, not the gate."""
        params = {"channel": channel_id, "text": text,
                  "metadata": json.dumps({"event_type": METADATA_EVENT,
                                          "event_payload": {"key": key}})}
        if thread_id is not None:
            params["thread_ts"] = str(thread_id)
        payload = self._api("chat.postMessage", **params)
        ts = payload.get("ts")
        if not ts:
            raise ApiError("chat.postMessage", "ok response carrying no ts")
        self._sent[key] = {"ts": ts, "channel": channel_id, "text": text,
                           "thread_id": thread_id}
        return {"ts": ts, "channel": payload.get("channel", channel_id), "key": key}

    def read_back(self, target, key, thread_id=None):
        """Proof of delivery BY KEY (channels/CONTRACT.md): the platform shows a message
        carrying this outbox key in its metadata. For a message this process posted the
        check is exact — fetched by its ts, key AND canonical text must match (guard 3:
        a read-back that found "a message" could not tell a truncated or rewritten
        post from the approved one). For a key this process never posted (recovery
        after a crash), the newest page of the channel — or the thread, when the
        caller passes `thread_id` — is searched for the key."""
        mine = self._sent.get(key)
        if mine is not None:
            found = self._fetch_exact(mine["channel"], mine["ts"], mine["thread_id"])
            return (found is not None and _metadata_key(found) == key
                    and _canonical(found.get("text", "")) == _canonical(mine["text"]))
        if thread_id is not None:
            candidates = self._replies_raw(target, str(thread_id))
        else:
            candidates = self._api("conversations.history", channel=target,
                                   limit=PAGE_LIMIT, include_all_metadata="true"
                                   ).get("messages", [])
        return any(_metadata_key(m) == key for m in candidates)

    def newest(self, channel_id, thread_id=None):
        """The newest message in the channel (top-level) or in one thread, normalized,
        or None when there is none — the outbox's pre-send re-read (ENH-31 guard 2): a
        message newer than the draft means the conversation moved on since the human
        approved the text, and the click must refuse with "re-read first"."""
        if thread_id is None:
            page = self._api("conversations.history", channel=channel_id, limit=1)
            msgs = page.get("messages", [])
            return self._normalize(channel_id, msgs[0]) if msgs else None
        replies = [m for m in self._replies_raw(channel_id, str(thread_id))
                   if m.get("ts") != str(thread_id)]
        if not replies:
            return None
        return self._normalize(channel_id, max(replies, key=lambda m: float(m["ts"])))

    def _fetch_exact(self, channel, ts, thread_id):
        if thread_id is not None:
            for m in self._replies_raw(channel, str(thread_id)):
                if m.get("ts") == ts:
                    return m
            return None
        page = self._api("conversations.history", channel=channel, latest=ts, oldest=ts,
                         inclusive="true", limit=1, include_all_metadata="true")
        for m in page.get("messages", []):
            if m.get("ts") == ts:
                return m
        return None

    def poll(self, cursor):
        """Gap-free and idempotent: same cursor -> same messages again, never fewer.

        All-or-nothing per call: any failure mid-pagination raises before a cursor is
        minted, so the engine re-polls the same window — duplicates are absorbed by
        the store/journal (R9), losses are not absorbable by anything.
        """
        offsets = json.loads(cursor) if cursor else {}
        if not isinstance(offsets, dict):
            raise ValueError(f"slack adapter: cursor is not a channel->ts object: "
                             f"{cursor!r} — refusing to guess a polling window")
        messages, new_offsets = [], dict(offsets)
        for channel in self.channels:
            fetched = self._history(channel, offsets.get(channel))
            messages.extend(fetched)
            if fetched:
                new_offsets[channel] = fetched[-1]["ts"]
        if new_offsets == offsets:
            return messages, cursor
        return messages, json.dumps(new_offsets, sort_keys=True)

    def resolve(self, ref):
        """Human ref <-> platform id, both directions (channels/CONTRACT.md)."""
        if _CHANNEL_ID.fullmatch(ref):
            ch = self._api("conversations.info", channel=ref)["channel"]
            return ch.get("name") or ref     # DMs have no name; the id is the answer
        if _USER_ID.fullmatch(ref):
            user = self._api("users.info", user=ref)["user"]
            profile = user.get("profile") or {}
            return (profile.get("display_name") or user.get("real_name")
                    or user.get("name") or ref)
        name = ref.lstrip("#@")
        found = self._find_channel(name)
        if found is None:
            found = self._find_user(name)
        if found is None:
            raise LookupError(f"slack adapter: cannot resolve {ref!r} to a channel or "
                              "user in this workspace")
        return found

    def health(self):
        """Cheap liveness via auth.test. CAN fail (contract rule 5), and reports any
        active 429 hold so 'the poller went quiet' is diagnosable from health output."""
        detail_suffix = self._hold_detail()
        try:
            who = self._api("auth.test", timeout=5.0)
        except ApiError as ex:
            return {"reachable": True, "auth_ok": False,
                    "detail": f"auth.test failed: {ex.error}{detail_suffix}"}
        except OSError as ex:
            return {"reachable": False, "auth_ok": False,
                    "detail": f"unreachable: {ex}{detail_suffix}"}
        return {"reachable": True, "auth_ok": True,
                "detail": f"auth.test ok as {who.get('user', '?')}{detail_suffix}"}

    # ---- transport ----------------------------------------------------------
    def _api(self, method, timeout=30.0, **params):
        if method not in READ_METHODS and method not in WRITE_METHODS:
            raise ReadOnlyViolation(method)
        status, headers, body = self.http(f"{self._api_base}/{method}", params, timeout)
        try:
            payload = json.loads(body) if body else {}
        except ValueError:
            payload = {}
        if status == 429 or (payload and payload.get("error") == "ratelimited"):
            # The wait is the PLATFORM'S number, exactly (ENH-1). The default only
            # covers a proxy that stripped the header; Slack itself always sends it.
            ex = RateLimited(_header(headers, "Retry-After", "1"), method=method)
            self._holds[method] = self._clock() + ex.retry_after
            raise ex
        if not payload:
            raise ApiError(method, f"HTTP {status} with a non-JSON body")
        if not payload.get("ok"):
            raise ApiError(method, payload.get("error", "unknown_error"))
        return payload

    def _urllib_http(self, url, form, timeout):
        """(status, headers, body) from a real POST. The token travels only in the
        Authorization header — never in the form, where it would leak into logs."""
        req = urllib.request.Request(
            url, data=urllib.parse.urlencode(form).encode(),
            headers={"Authorization": f"Bearer {self._token}",
                     "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as ex:
            # 4xx/5xx still carry Slack's JSON body and Retry-After header — they are
            # responses to interpret, not transport failures.
            return ex.code, dict(ex.headers), ex.read().decode("utf-8", "replace")

    # ---- polling internals ---------------------------------------------------
    def _history_raw(self, channel, oldest=None, latest=None):
        """Raw platform messages in the window, ALL pages followed to the end."""
        out, page_cursor = [], None
        while True:
            params = {"channel": channel, "limit": PAGE_LIMIT}
            if oldest is not None:
                params["oldest"] = oldest
            if latest is not None:
                params["latest"] = latest
            if page_cursor:
                params["cursor"] = page_cursor
            payload = self._api("conversations.history", **params)
            out.extend(payload.get("messages", []))
            if not payload.get("has_more"):
                break
            page_cursor = (payload.get("response_metadata") or {}).get("next_cursor")
            if not page_cursor:
                raise ApiError("conversations.history",
                               "has_more with no next_cursor — refusing to silently "
                               "drop the rest of the window")
        return out

    def _history(self, channel, oldest):
        """Every message newer than `oldest` (exclusive), oldest-first, ALL pages."""
        out = [self._normalize(channel, m) for m in self._history_raw(channel, oldest)]
        out.sort(key=lambda m: float(m["ts"]))
        return out

    # ---- optional capability: thread replies (channels/CONTRACT.md, ENH-30) -----
    def replies(self, channel, thread_ts, oldest=None):
        """Every reply under one thread newer than `oldest` (exclusive), oldest-first,
        ALL pages or raise — never the parent.

        conversations.history does not serve thread replies (a parent arrives with a
        reply_count and nothing else), so without this method an in-thread answer is
        invisible to the engine. Measured on the aws-do-agent integration 2026-10-12:
        every ask read UNANSWERED after the agent had answered each one in its thread.
        The parent rides the first page of conversations.replies and is dropped here:
        it is the message being answered, not an answer.
        """
        raw = self._replies_raw(channel, thread_ts, oldest)
        out = [self._normalize(channel, m) for m in raw if m.get("ts") != thread_ts]
        out.sort(key=lambda m: float(m["ts"]))
        return out

    def _replies_raw(self, channel, thread_ts, oldest=None):
        out, page_cursor = [], None
        while True:
            params = {"channel": channel, "ts": thread_ts, "limit": PAGE_LIMIT}
            if oldest is not None:
                params["oldest"] = oldest
            if page_cursor:
                params["cursor"] = page_cursor
            payload = self._api("conversations.replies", **params)
            out.extend(payload.get("messages", []))
            if not payload.get("has_more"):
                break
            page_cursor = (payload.get("response_metadata") or {}).get("next_cursor")
            if not page_cursor:
                raise ApiError("conversations.replies",
                               "has_more with no next_cursor — refusing to silently "
                               "drop the rest of the thread")
        return out

    # ---- optional capability: the platform snapshot (channels/CONTRACT.md) ------
    def retrievable_ts(self, channel, oldest=None, latest=None):
        """Every ts the platform will serve for this channel RIGHT NOW.

        The parity differ needs a third opinion because the incumbent oracle is an archive,
        not ground truth: it holds messages that have since been deleted, and the engine —
        which can only ever fetch what the API serves — is then blamed for "losing" them.
        Measured 2026-08-26: 342 of 507 oracle rows in one channel were unretrievable (one
        app's deleted burst), and without this method the differ read that as a read-path
        defect (core/parity.py's docstring carries the numbers).

        Deliberately a plain ts SET, not messages: it is evidence for a comparison, not
        content to ingest, and keeping it small means the snapshot can be written to disk
        and reviewed after the fact.
        """
        return {m["ts"] for m in self._history_raw(channel, oldest, latest) if m.get("ts")}

    @staticmethod
    def _normalize(channel_id, m):
        ts = m.get("ts")
        thread = m.get("thread_ts")
        return {
            "channel_type": "slack",
            "channel_id": channel_id,
            # The store REQUIRES a sender (R5). Bot/system rows carry bot_id or
            # nothing; mapping to a placeholder keeps the row rather than dropping it.
            "sender_id": m.get("user") or m.get("bot_id") or "unknown",
            "sender_name": m.get("username"),
            "ts": ts,
            "text": m.get("text", ""),
            # Slack marks a thread PARENT with thread_ts == its own ts; the contract
            # wants null for top-level messages.
            "thread_id": thread if (thread and thread != ts) else None,
            "attachments": _attachments(m),
            "raw": json.dumps(m, sort_keys=True),
        }

    # ---- resolve internals ---------------------------------------------------
    def _find_channel(self, name):
        for page in self._pages("conversations.list", "channels",
                                types="public_channel,private_channel"):
            for ch in page:
                if ch.get("name") == name:
                    return ch["id"]
        return None

    def _find_user(self, name):
        for page in self._pages("users.list", "members"):
            for user in page:
                profile = user.get("profile") or {}
                if name in (user.get("name"), user.get("real_name"),
                            profile.get("display_name")):
                    return user["id"]
        return None

    def _pages(self, method, key, **params):
        page_cursor = None
        while True:
            call = dict(params, limit=PAGE_LIMIT)
            if page_cursor:
                call["cursor"] = page_cursor
            payload = self._api(method, **call)
            yield payload.get(key, [])
            page_cursor = (payload.get("response_metadata") or {}).get("next_cursor")
            if not page_cursor:
                return

    # ---- health internals ------------------------------------------------------
    def _hold_detail(self):
        active = {m: t - self._clock() for m, t in self._holds.items()
                  if t > self._clock()}
        if not active:
            return ""
        holds = ", ".join(f"{m} ready in {s:.0f}s" for m, s in sorted(active.items()))
        return f"; rate-limit backoff: {holds}"
