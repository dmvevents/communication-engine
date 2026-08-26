"""channels/slack — the read-only Slack adapter (ENH-18).

First REAL platform adapter, landed as a pure dir-drop (R11: zero core/ changes). The
operator authorized the workspace token READ-ONLY, so read-only is enforced in layers,
not promised in prose:

* there is no `send`, `read_back`, or any delivery primitive on this class — an outbox
  pointed at this adapter has nothing to drive;
* `_api`, the one funnel every request passes through, default-DENIES: any Web API
  method outside READ_METHODS is refused before a byte reaches the wire. Without this,
  the no-send property would be one convenience wrapper away from being lost.

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
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from core.ratelimit import RateLimited

API_BASE = "https://slack.com/api"
PAGE_LIMIT = 200          # Slack's recommended per-page maximum for history/list reads

# Deny-by-default: everything this adapter is allowed to call, exhaustively. Extending
# the adapter means extending this list IN THE SAME DIFF as the test that uses it.
READ_METHODS = frozenset({
    "auth.test",
    "conversations.history",
    "conversations.info",
    "conversations.list",
    "users.info",
    "users.list",
})

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
    """A non-read method reached the transport. This adapter is read-only BY
    AUTHORIZATION (the operator granted the token for reading, 2026-08-26); the
    correct fix is never to widen this list quietly — send lives behind core/outbox
    in a future, separately-authorized adapter."""

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


class Adapter:
    def __init__(self, auth=None, http=None, clock=None):
        auth = auth or {}
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

    # ---- contract surface --------------------------------------------------
    def capabilities(self):
        # send is False and stays False: reply policy for this platform is
        # deny-by-default and the send authorization simply does not exist yet.
        return {"read": True, "history": True, "search": False,
                "send": False, "react": False, "threads": False}

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
        if method not in READ_METHODS:
            raise ReadOnlyViolation(method)
        status, headers, body = self.http(f"{API_BASE}/{method}", params, timeout)
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
    def _history(self, channel, oldest):
        """Every message newer than `oldest` (exclusive), oldest-first, ALL pages."""
        out, page_cursor = [], None
        while True:
            params = {"channel": channel, "limit": PAGE_LIMIT}
            if oldest is not None:
                params["oldest"] = oldest
            if page_cursor:
                params["cursor"] = page_cursor
            payload = self._api("conversations.history", **params)
            out.extend(self._normalize(channel, m)
                       for m in payload.get("messages", []))
            if not payload.get("has_more"):
                break
            page_cursor = (payload.get("response_metadata") or {}).get("next_cursor")
            if not page_cursor:
                raise ApiError("conversations.history",
                               "has_more with no next_cursor — refusing to silently "
                               "drop the rest of the window")
        out.sort(key=lambda m: float(m["ts"]))
        return out

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
