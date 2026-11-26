"""channels/telegram — the read-only Telegram bot adapter (ENH-25).

First NON-Slack platform, landed as a pure dir-drop (R11: zero core/ changes). Like
channels/slack, read-only is enforced in layers, not promised in prose: no send,
read_back, or delivery primitive exists on this class, and `_api` — the one funnel every
request passes through — default-DENIES any Bot API method outside READ_METHODS before a
byte reaches the wire. The incumbent host's live HMAC bridge and its reply path are
explicitly out of scope; this adapter is a fresh, config-driven client.

The platform finding that shapes everything here (state/DESIGN-ENH-25-telegram.md):
**Telegram bots have no history API.** `getUpdates` is a queue, and its `offset`
parameter is an acknowledgement — calling getUpdates(offset=N) permanently confirms
every update below N, and those updates are gone from the platform forever. Four
structural consequences, each pinned by tests/test_telegram_adapter.py:

* The offset sent is EXACTLY the cursor the engine handed back — the one durable
  acknowledgement, committed only after the store/journal write (core/schedule.py's
  cursor-after-journal ordering). On Slack that ordering is prudence; here it is the
  only correct implementation, because the platform destroys the re-read window as the
  offset advances. The adapter never remembers its own high-water mark.
* poll() makes exactly ONE destructive read per call. In-poll pagination would advance
  the offset past messages the engine has not committed — a crash mid-drain loses them
  at the platform, unrecoverably. A backlog longer than one page (PAGE_LIMIT) drains
  across successive polls, each ratcheting the cursor only after the journal write.
* One instance watches exactly ONE chat. The engine keys cursors per (instance,
  channel), but the bot queue has a single ack authority: with two chats, chat A's
  committed cursor covers update ids whose chat-B messages were filtered out of A's
  poll iteration, so a crash between the two per-channel commits lets A's next offset
  acknowledge B's un-journaled messages. Refused at construction; the fix is one bot
  per chat (which Telegram's single-consumer getUpdates rule wants anyway). Updates
  from other chats the bot is in are not ingested (the socket adapter's
  watched-channels rule) but do advance the cursor — otherwise foreign chatter wedges
  the poll loop forever.
* `retrievable_ts` is IMPOSSIBLE — a bot cannot ask for anything it has acknowledged —
  so it is deliberately ABSENT, never stubbed (an empty snapshot would mark every
  still-real message "deleted upstream", the direction that hides a real loss). Parity
  against this store is permanently fail-closed and core/parity's ENH-27 declaration
  says so by name.

`ts` is the message_id, not message.date: the store keys rows (channel_type,
channel_id, ts) and date is whole seconds, so two burst messages would merge into one
row. message_id is the platform's per-chat monotonic sequence — unique, sortable within
the chat, and an edited_message carries the SAME message_id, so an edit lands as a
revision of the original row (R23) exactly as Slack's edit-in-place does. Wall-clock
time stays available in `raw`.

Rate limits (contract rule 3): Telegram's 429 carries its wait in the JSON body
(`parameters.retry_after`, seconds) — not the Retry-After header — so the body is read
first; a helper that only read headers would silently invent a wait. The exception
names the method (ENH-1's (instance, method)-keyed back-off) and active holds are
reported through health().detail.

The bot token and chat id arrive as `env:NAME` references via config
(`auth: {"token": ..., "channels": ...}`) — real chat ids are host-specific literals
the sanitize gate refuses in committed files. The token rides the URL path (the Bot API
protocol gives no header alternative), so transport errors are always converted to
(status, headers, body) tuples and never re-raised carrying the URL.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from core.ratelimit import RateLimited

API_BASE = "https://api.telegram.org"
PAGE_LIMIT = 100          # the Bot API's getUpdates maximum per call

# Deny-by-default: everything this adapter is allowed to call, exhaustively. Extending
# the adapter means extending this list IN THE SAME DIFF as the test that uses it.
READ_METHODS = frozenset({
    "getMe",
    "getUpdates",
    "getChat",
})

# Pinned at the PLATFORM via allowed_updates: the queue only ever holds what the
# adapter ingests, so nothing arrives here to be silently dropped and destroyed on the
# next ack. edited_message is ingested because it carries the original message_id —
# the engine's revision seam (R23) — not a new row.
INGEST_UPDATES = ("message", "edited_message")

# Chat ids are numeric and may be NEGATIVE (groups/supergroups) — a leading '-' has
# bitten id-shaped regexes before; Telegram gets its own shape, never a widened Slack one.
_CHAT_ID = re.compile(r"-?\d+")

# Media a message can carry INSTEAD of text (ENH-4: attachments are content). photo is
# special-cased below (one image at several resolutions); these map to kind "file".
_MEDIA_KEYS = ("document", "voice", "audio", "video", "video_note", "animation",
               "sticker")


class ApiError(RuntimeError):
    """Telegram answered but the call failed (ok:false, or an unusable response body).
    Carries method + description and never the token."""

    def __init__(self, method: str, error: str):
        self.method = method
        self.error = error
        super().__init__(f"{method} failed: {error}")


class ReadOnlyViolation(RuntimeError):
    """A non-read method reached the transport. This adapter is read-only BY DESIGN
    (ENH-25 lands ingestion only; no send authorization exists for this platform);
    the correct fix is never to widen this list quietly — send lives behind
    core/outbox in a future, separately-authorized adapter."""

    def __init__(self, method: str):
        super().__init__(
            f"{method} is not a read method — this adapter is read-only and refuses "
            f"anything outside its allowlist before any I/O happens")


def _header(headers: dict, name: str, default=None):
    """HTTP header lookup, case-insensitive — proxies re-case them freely."""
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v
    return default


def _attachments(m):
    """Media descriptors for everything a message can carry instead of text.

    `url` is ALWAYS None: Telegram file downloads go through getFile to a URL that
    embeds the bot token (api.telegram.org/file/bot<TOKEN>/...), and a url field here
    would persist a live credential into every adopter's store. The file_id in `raw`
    is the retrieval handle for a consumer that is authorized to mint one.
    """
    out = []
    if m.get("photo"):
        # photo[] is ONE image at several resolutions, not several attachments.
        out.append({"kind": "image", "name": None, "mimetype": None, "url": None})
    for key in _MEDIA_KEYS:
        media = m.get(key)
        if media:
            out.append({"kind": "file", "name": media.get("file_name"),
                        "mimetype": media.get("mime_type"), "url": None})
    return out


class Adapter:
    def __init__(self, auth=None, http=None, clock=None):
        auth = auth or {}
        self._token = auth.get("token")
        if not self._token:
            raise ValueError("telegram adapter: auth['token'] is missing — configure "
                             "it as an env:NAME reference in settings.json")
        raw_channels = auth.get("channels") or ""
        chats = tuple(c.strip() for c in raw_channels.split(",") if c.strip())
        if not chats:
            raise ValueError("telegram adapter: auth['channels'] is missing or empty "
                             "— the numeric chat id to watch (env:NAME reference), or "
                             "poll() would silently watch nothing")
        if len(chats) != 1:
            # The engine keys cursors per (instance, channel); this bot queue has ONE
            # ack authority. Two chats sharing it means one chat's committed cursor
            # can acknowledge the other's un-journaled messages (crash between the two
            # per-channel commits in a scheduler cycle) — a silent, unrecoverable loss.
            raise ValueError(
                "telegram adapter: exactly one chat per instance — getUpdates is a "
                "single destructive queue and the engine's per-channel cursor commits "
                "cannot share its acknowledgement safely. Run one bot (one instance) "
                f"per chat instead of sharing {len(chats)} chats on one token")
        if not _CHAT_ID.fullmatch(chats[0]):
            # '@name' here would poll successfully and match nothing: getUpdates rows
            # carry numeric chat ids and the engine compares channel_id strings
            # exactly — the silent watch-nothing failure (the two-place lesson).
            raise ValueError(f"telegram adapter: auth['channels'] must be the numeric "
                             f"chat id (groups are negative), got {chats[0]!r}")
        self.channels = chats
        self.http = http or self._urllib_http
        self._clock = clock or time.monotonic
        self._holds = {}   # method -> monotonic instant its 429 hold expires (report-only)

    # ---- contract surface --------------------------------------------------
    def capabilities(self):
        # history is False and it is the honest headline: there is no history API, so
        # nothing that was acknowledged can ever be re-read. send is False and stays
        # False — no send authorization exists for this platform.
        return {"read": True, "history": False, "search": False,
                "send": False, "react": False, "threads": True}

    def poll(self, cursor):
        """One getUpdates call at the engine's committed cursor — never more.

        Gap-free BECAUSE of that restraint: offset=N acknowledges (destroys) every
        update below N, and the given cursor is the only N known to be journaled
        (core/schedule.py commits it strictly after the store write). Re-polling with
        the same cursor re-serves the unacknowledged window — duplicates, never a loss.
        """
        if cursor is not None and not _CHAT_ID.fullmatch(str(cursor)):
            raise ValueError(f"telegram adapter: cursor is not an update-id offset: "
                             f"{cursor!r} — refusing to guess one; a guessed offset "
                             "acknowledges (destroys) unread messages")
        params = {"limit": PAGE_LIMIT, "timeout": 0,
                  "allowed_updates": json.dumps(list(INGEST_UPDATES))}
        if cursor is not None:
            params["offset"] = cursor
        payload = self._api("getUpdates", **params)
        batch = payload.get("result") or []
        if not batch:
            return [], cursor
        messages = []
        for update in batch:
            m = update.get("message") or update.get("edited_message") or {}
            # Foreign chats the bot is in are not ingested (watched-channels rule) but
            # their update ids still count toward the cursor below — a wedged offset
            # would re-read their chatter forever and never reach the watched chat.
            if str((m.get("chat") or {}).get("id")) == self.channels[0]:
                messages.append(self._normalize(update, m))
        # Stable sort by the per-chat sequence: an edit re-serves an OLD ts on purpose
        # (that is the revision identity), and stability keeps an original that shares
        # a batch with its own edit in arrival order.
        messages.sort(key=lambda m: int(m["ts"]))
        return messages, str(batch[-1]["update_id"] + 1)

    def resolve(self, ref):
        """Human ref <-> platform id, both directions (channels/CONTRACT.md)."""
        if _CHAT_ID.fullmatch(ref):
            chat = self._chat(ref)
            return (chat.get("title") or chat.get("username")
                    or chat.get("first_name") or ref)
        try:
            chat = self._chat("@" + ref.lstrip("@"))
        except ApiError as ex:
            # Only public @usernames are addressable; a private chat has no handle
            # the Bot API will look up.
            raise LookupError(f"telegram adapter: cannot resolve {ref!r} to a chat "
                              f"({ex.error})") from None
        return str(chat["id"])

    def health(self):
        """Cheap liveness via getMe. CAN fail (contract rule 5), and reports any
        active 429 hold so 'the poller went quiet' is diagnosable from health output.
        Never getUpdates: that queue is single-consumer (a concurrent caller 409s the
        live poll) and its reads are the one destructive surface in this adapter.
        """
        detail_suffix = self._hold_detail()
        try:
            me = self._api("getMe", http_timeout=5.0)
        except ApiError as ex:
            return {"reachable": True, "auth_ok": False,
                    "detail": f"getMe failed: {ex.error}{detail_suffix}"}
        except OSError as ex:
            return {"reachable": False, "auth_ok": False,
                    "detail": f"unreachable: {ex}{detail_suffix}"}
        who = (me.get("result") or {}).get("username", "?")
        return {"reachable": True, "auth_ok": True,
                "detail": f"getMe ok as @{who}{detail_suffix}"}

    # ---- transport ----------------------------------------------------------
    def _api(self, method, http_timeout=30.0, **params):
        # http_timeout is a separate name because "timeout" is a real Bot API form
        # field (getUpdates long-poll seconds) — a shared kwarg would shadow it.
        if method not in READ_METHODS:
            raise ReadOnlyViolation(method)
        status, headers, body = self.http(
            f"{API_BASE}/bot{self._token}/{method}", params, http_timeout)
        try:
            payload = json.loads(body) if body else {}
        except ValueError:
            payload = {}
        if status == 429 or (payload and payload.get("error_code") == 429):
            # The wait is the PLATFORM'S number, exactly (ENH-1) — and Telegram puts
            # it in the BODY (parameters.retry_after), not the Retry-After header, so
            # the body is read first. The header covers a proxy that stripped the
            # body; the final "1" covers one that stripped both.
            wait = (payload.get("parameters") or {}).get("retry_after")
            if wait is None:
                wait = _header(headers, "Retry-After", "1")
            ex = RateLimited(wait, method=method)
            self._holds[method] = self._clock() + ex.retry_after
            raise ex
        if not payload:
            raise ApiError(method, f"HTTP {status} with a non-JSON body")
        if not payload.get("ok"):
            raise ApiError(method, payload.get("description", "unknown_error"))
        return payload

    def _urllib_http(self, url, form, timeout):
        """(status, headers, body) from a real POST. The token rides the URL path —
        the Bot API's protocol — so HTTPError is always converted to a tuple here and
        never re-raised: its str() carries the full URL, token included."""
        req = urllib.request.Request(
            url, data=urllib.parse.urlencode(form).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as ex:
            # 4xx/5xx still carry Telegram's JSON body (description, parameters) —
            # they are responses to interpret, not transport failures.
            return ex.code, dict(ex.headers), ex.read().decode("utf-8", "replace")

    # ---- polling internals ---------------------------------------------------
    def _normalize(self, update, m):
        sender = m.get("from") or m.get("sender_chat") or {}
        reply = m.get("reply_to_message") or {}
        name = sender.get("username") or sender.get("first_name")
        return {
            "channel_type": "telegram",
            "channel_id": str(m["chat"]["id"]),
            # The store REQUIRES a sender (R5). Anonymous/channel-style rows carry
            # sender_chat or nothing; a placeholder keeps the row, never drops it.
            "sender_id": str(sender.get("id", "unknown")),
            "sender_name": name,
            # message_id, NOT date: the store keys (channel_type, channel_id, ts) and
            # date is whole seconds — burst messages would merge. An edit re-serves
            # the same message_id, landing as a revision of this row (R23).
            "ts": str(m["message_id"]),
            "text": m.get("text") or m.get("caption") or "",
            # Reply-chain threading: the parent's message_id IS the parent's ts, the
            # same alignment Slack's thread_ts gives core for free. Forum-topic
            # message_thread_id is NOT read — a topic id is not a row this store has.
            "thread_id": (str(reply["message_id"]) if reply.get("message_id")
                          else None),
            "attachments": _attachments(m),
            # The whole update, not just the message: update_id is the queue position
            # the cursor acknowledges — audit needs it next to the content.
            "raw": json.dumps(update, sort_keys=True),
        }

    # ---- resolve internals ---------------------------------------------------
    def _chat(self, chat_id):
        return self._api("getChat", chat_id=chat_id).get("result") or {}

    # ---- health internals ------------------------------------------------------
    def _hold_detail(self):
        active = {m: t - self._clock() for m, t in self._holds.items()
                  if t > self._clock()}
        if not active:
            return ""
        holds = ", ".join(f"{m} ready in {s:.0f}s" for m, s in sorted(active.items()))
        return f"; rate-limit backoff: {holds}"
