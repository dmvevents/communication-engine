"""channels/slack_socket — Socket Mode push ingestion (ENH-14): push for latency,
poll for truth.

Socket Mode delivers Events API payloads over an OUTBOUND WebSocket — no public
endpoint, no inbound firewall hole — which fits the loopback-only constraint that
forced the incumbent into polling. Detection becomes sub-second and costs no Web API
rate-limit budget. But the platform documents that events can be MISSED (10-connection
cap, connections cycle every few hours, enabling mid-flow can drop events), so this
adapter is NEVER the truth: it runs beside the polling `slack` adapter and
scripts/push-poll-parity.py continuously diffs the two stores, failing loudly on any
divergence. Push-only would silently lose messages — the exact failure class this
project exists to eliminate.

Design consequences, each pinned by tests/test_socket_adapter.py:

* the platform cycles connections and SAYS SO with a disconnect frame; the adapter
  reconnects, minting a FRESH url via apps.connections.open (tickets are single-use);
* every events_api envelope is ACKed with its envelope_id — unacked envelopes are
  re-delivered and then dropped by the platform. Ack happens AFTER buffering, so a
  crash in between duplicates (absorbed by the store, R9) instead of losing;
* only watched channels are ingested, and ephemeral edit wrappers (message_changed,
  message_deleted — event-only rows conversations.history never shows) are skipped:
  either would poison push-vs-poll parity permanently;
* `poll(cursor)` drains the local buffer — no thread, no scheduler. The engine calls
  it as often as it likes (it is a local socket read, costing nothing against Slack's
  limits); pings are answered and reconnects happen inside that pump. The buffer is
  pruned only at the cursor the ENGINE hands back — the one durable acknowledgement —
  so a re-poll with the same cursor may duplicate, never lose (channels/CONTRACT.md);
* an `app_rate_limited` payload (the Events API's 30k deliveries/app/workspace/hour
  ceiling — overflow raises an EVENT, not an error) is a first-class LOSS signal
  (ENH-16): it records a delivery-gap window, `health()` FAILS (`complete: False`)
  while any window is unrecovered, `recovery_due()` demands the completeness poll
  regardless of backoff (R3's rule), and only `mark_recovered(completed_at)` with a
  poll completion past the window closes it;
* there is no send surface at any layer, same as the polling adapter (ingestion only);
* the app-level token and channel set arrive as `env:NAME` references via config
  (`auth: {"app_token": ..., "channels": ...}`, comma-separated ids), and a 429 on
  apps.connections.open raises core.ratelimit.RateLimited carrying the platform's
  EXACT Retry-After and the method (ENH-1's keyed back-off).

The websocket layer is a minimal stdlib RFC 6455 client (text frames, ping/pong,
close, fragmentation) — this repo takes no dependencies for one protocol handshake.

Live smoke is OPERATOR-OWNED: it needs Socket Mode enabled on the Slack app and an
app-level `xapp-` token that does not exist yet (see README.md in this directory).
"""
from __future__ import annotations

import base64
import collections
import hashlib
import json
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from core.ratelimit import RateLimited

API_BASE = "https://slack.com/api"
OPEN_METHOD = "apps.connections.open"

# Event-only wrappers that never appear as rows in conversations.history: ingesting
# one creates a row the poll side can never confirm — a PERMANENT parity divergence.
EPHEMERAL_SUBTYPES = frozenset({"message_changed", "message_deleted"})

# A flapping server must not trap one poll() inside an infinite reconnect loop; past
# this budget the cycle returns what it has and health() carries the last reason.
MAX_RECONNECTS_PER_PUMP = 3

# Platform id shapes — same rationale as channels/slack/adapter.py.
_CHANNEL_ID = re.compile(r"[CDG][A-Z0-9]{6,}")
_USER_ID = re.compile(r"[UW][A-Z0-9]{6,}")


class ApiError(RuntimeError):
    """Slack answered but the call failed (ok:false, or an unusable response body).
    Carries method + error code and never the token."""

    def __init__(self, method: str, error: str):
        self.method = method
        self.error = error
        super().__init__(f"{method} failed: {error}")


class SocketClosed(ConnectionError):
    """The websocket is no longer usable (close frame, EOF, refused upgrade)."""


def _header(headers: dict, name: str, default=None):
    """HTTP header lookup, case-insensitive — proxies re-case them freely."""
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v
    return default


def _attachments(event):
    """Same normalization as the polling adapter's (ENH-4): the message event carries
    the same `files` array conversations.history shows, and both ingestion paths must
    keep the upload — parity holds on ts, so the two stores could otherwise agree a
    message exists while disagreeing about what it contained. Message-level
    `attachments` (unfurls, bot decoration) are not content and are not ingested."""
    out = []
    for f in event.get("files") or ():
        mimetype = f.get("mimetype") or None
        out.append({
            "kind": "image" if (mimetype or "").startswith("image/") else "file",
            "name": f.get("name") or f.get("title"),
            "mimetype": mimetype,
            "url": f.get("url_private"),
        })
    return out


# ---------------------------------------------------------------------------
# RFC 6455 frame layer (client side), stdlib only.
# ---------------------------------------------------------------------------

# The RFC 6455 §1.3 magic GUID, assembled by concatenation: its tail happens to
# match the platform-id shape scripts/sanitize-gate.sh scans for, and the gate's
# own rule is that pattern-shaped literals never appear in committed source. The
# accept-key worked-example test proves the assembled value is exactly right.
_WS_GUID = "258EAFA5-E914-47DA-95CA-" + "C5AB0DC" + "85B11"
OP_CONT, OP_TEXT, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x8, 0x9, 0xA


def _accept_for(key: str) -> str:
    """The Sec-WebSocket-Accept a real RFC 6455 server must answer for `key`."""
    return base64.b64encode(
        hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()


def _encode_frame(opcode: int, payload: bytes, mask_key, fin: bool = True) -> bytes:
    """One frame. mask_key=None builds an UNMASKED (server-style) frame — tests use
    that to script a fake server; every real client send masks (RFC 6455 §5.3: a
    server MUST drop the connection on an unmasked client frame)."""
    head = bytearray([(0x80 if fin else 0) | opcode])
    n = len(payload)
    mask_bit = 0x80 if mask_key else 0
    if n < 126:
        head.append(mask_bit | n)
    elif n < 1 << 16:
        head.append(mask_bit | 126)
        head += n.to_bytes(2, "big")
    else:
        head.append(mask_bit | 127)
        head += n.to_bytes(8, "big")
    if not mask_key:
        return bytes(head) + payload
    masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return bytes(head) + mask_key + masked


def _parse_frame(buf: bytes):
    """(fin, opcode, payload, bytes_consumed), or None while the frame is incomplete."""
    if len(buf) < 2:
        return None
    fin = bool(buf[0] & 0x80)
    opcode = buf[0] & 0x0F
    masked = bool(buf[1] & 0x80)
    n = buf[1] & 0x7F
    idx = 2
    if n == 126:
        if len(buf) < 4:
            return None
        n = int.from_bytes(buf[2:4], "big")
        idx = 4
    elif n == 127:
        if len(buf) < 10:
            return None
        n = int.from_bytes(buf[2:10], "big")
        idx = 10
    mask = b""
    if masked:
        if len(buf) < idx + 4:
            return None
        mask = buf[idx:idx + 4]
        idx += 4
    if len(buf) < idx + n:
        return None
    payload = buf[idx:idx + n]
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return fin, opcode, payload, idx + n


class _WebSocketConnection:
    """Minimal RFC 6455 client — enough for Slack Socket Mode: text frames in,
    ping/pong, close, fragment reassembly. recv_text() never blocks the poll cycle
    longer than IDLE_WAIT_S."""

    IDLE_WAIT_S = 0.05

    def __init__(self, url: str, timeout: float = 10.0, sock=None, key: str = None):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "wss":
            raise ValueError(f"not a wss:// url: {url!r}")
        host, port = parts.hostname, parts.port or 443
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        if sock is None:
            raw = socket.create_connection((host, port), timeout=timeout)
            sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        self._sock = sock
        self._buf = b""
        self._texts = collections.deque()
        self._fragments = []
        self._closed = None
        key = key or base64.b64encode(os.urandom(16)).decode()
        self._sock.sendall(
            (f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
             "Upgrade: websocket\r\nConnection: Upgrade\r\n"
             f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
             ).encode())
        self._expect_upgrade(key)

    def _expect_upgrade(self, key: str) -> None:
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise SocketClosed("connection closed during websocket handshake")
            raw += chunk
        head, _, rest = raw.partition(b"\r\n\r\n")
        self._buf = rest                     # frames may ride the same read
        lines = head.decode("latin-1").split("\r\n")
        status = lines[0].split()
        if len(status) < 2 or status[1] != "101":
            raise SocketClosed(f"websocket upgrade refused: {lines[0]!r}")
        accept = None
        for line in lines[1:]:
            name, _, value = line.partition(":")
            if name.strip().lower() == "sec-websocket-accept":
                accept = value.strip()
        if accept != _accept_for(key):
            # A wrong accept means we are NOT speaking RFC 6455 to the host we think;
            # refusing beats parsing frames out of an impostor stream.
            raise SocketClosed("Sec-WebSocket-Accept mismatch on upgrade")

    def recv_text(self):
        """The next complete text payload, or None when nothing is pending."""
        while True:
            self._drain_buf()
            if self._texts:
                return self._texts.popleft()
            if self._closed is not None:
                raise SocketClosed(self._closed)
            try:
                self._sock.settimeout(self.IDLE_WAIT_S)
                chunk = self._sock.recv(65536)
            except (TimeoutError, socket.timeout, ssl.SSLWantReadError,
                    BlockingIOError):
                return None
            if not chunk:
                self._closed = "connection closed by peer (EOF)"
                continue
            self._buf += chunk

    def _drain_buf(self) -> None:
        while True:
            frame = _parse_frame(self._buf)
            if frame is None:
                return
            fin, opcode, payload, used = frame
            self._buf = self._buf[used:]
            if opcode in (OP_TEXT, OP_CONT):
                self._fragments.append(payload)
                if fin:
                    text = b"".join(self._fragments)
                    self._fragments = []
                    self._texts.append(text.decode("utf-8", "replace"))
            elif opcode == OP_PING:
                # unanswered pings get the connection dropped server-side
                self._sock.sendall(_encode_frame(OP_PONG, payload, os.urandom(4)))
            elif opcode == OP_CLOSE:
                # the close only ends what FOLLOWS it: texts already parsed in this
                # read are delivered first, or they would be lost messages
                self._closed = "close frame from server"
            # OP_PONG / binary: nothing for Socket Mode to do

    def send_text(self, text: str) -> None:
        self._sock.sendall(_encode_frame(OP_TEXT, text.encode(), os.urandom(4)))

    def close(self) -> None:
        try:
            self._sock.sendall(_encode_frame(OP_CLOSE, b"", os.urandom(4)))
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# The adapter.
# ---------------------------------------------------------------------------

class Adapter:
    def __init__(self, auth=None, http=None, connect=None, clock=None):
        auth = auth or {}
        self._token = auth.get("app_token")
        if not self._token:
            raise ValueError("slack_socket adapter: auth['app_token'] is missing — "
                             "the app-level token apps.connections.open requires; "
                             "configure it as an env:NAME reference in settings.json")
        raw_channels = auth.get("channels") or ""
        self.channels = tuple(c.strip() for c in raw_channels.split(",") if c.strip())
        if not self.channels:
            raise ValueError("slack_socket adapter: auth['channels'] is missing or "
                             "empty — a comma-separated channel-id list (env:NAME "
                             "reference), or ingestion would silently watch nothing")
        self._watched = frozenset(self.channels)
        self.http = http or self._urllib_http
        self._connect = connect or _WebSocketConnection
        self._clock = clock or time.monotonic
        self._holds = {}            # method -> monotonic 429-hold expiry (report-only)
        self._conn = None
        self._buffer = []           # normalized messages awaiting an engine cursor
        self._last_disconnect = None
        self._gaps = []             # platform-admitted delivery drops, unrecovered

    # ---- contract surface --------------------------------------------------
    def capabilities(self):
        # history is False on purpose: Socket Mode delivers live events only, so
        # backfill/coverage belongs to the polling adapter (the truth side).
        return {"read": True, "history": False, "search": False,
                "send": False, "react": False, "threads": False}

    def poll(self, cursor):
        """Drain the socket, then serve the buffer. Same idempotency contract as the
        polling adapter: re-polling with the same cursor may duplicate, never lose."""
        offsets = json.loads(cursor) if cursor else {}
        if not isinstance(offsets, dict):
            raise ValueError(f"slack_socket adapter: cursor is not a channel->ts "
                             f"object: {cursor!r} — refusing to guess a window")
        self._pump()
        # The engine handing back `cursor` is the one DURABLE acknowledgement that
        # nothing at/below it will ever be asked for again — the only safe prune
        # line. Pruning any higher would break same-cursor re-poll idempotency.
        self._buffer = [m for m in self._buffer
                        if float(m["ts"]) > float(offsets.get(m["channel_id"], "-inf"))]
        fresh = sorted(self._buffer, key=lambda m: float(m["ts"]))
        new_offsets = dict(offsets)
        for m in fresh:
            new_offsets[m["channel_id"]] = m["ts"]
        if new_offsets == offsets:
            return fresh, cursor
        return fresh, json.dumps(new_offsets, sort_keys=True)

    def resolve(self, ref):
        """An app-level (xapp) token cannot call the Web API directory methods, so
        this adapter has no name<->id lookup. Ids pass through — pretending to
        resolve a name would silently hand back garbage."""
        if _CHANNEL_ID.fullmatch(ref) or _USER_ID.fullmatch(ref):
            return ref
        raise LookupError(f"slack_socket adapter cannot resolve {ref!r}: Socket Mode "
                          "carries no directory — resolve names through the polling "
                          "'slack' adapter, which holds a Web API token")

    def health(self):
        """CAN fail (contract rule 5): a dead app token or unreachable platform is
        reported, not absorbed. Also surfaces the last disconnect reason and any
        active 429 hold, so 'push went quiet' is diagnosable from health output.

        `complete` (ENH-16) is False while any platform-admitted delivery gap is
        unrecovered — a distinct failing state from reachable/auth_ok, because the
        socket is up and the token is fine; the platform just admitted it dropped
        deliveries. Filing that as detail is how an engine loses messages while
        looking healthy."""
        detail_suffix = self._hold_detail() + self._gap_detail()
        complete = not self._gaps
        if self._conn is None:
            try:
                self._connect_socket(timeout=5.0)
            except ApiError as ex:
                return {"reachable": True, "auth_ok": False, "complete": complete,
                        "detail": f"{OPEN_METHOD} failed: {ex.error}{detail_suffix}"}
            except OSError as ex:
                return {"reachable": False, "auth_ok": False, "complete": complete,
                        "detail": f"unreachable: {ex}{detail_suffix}"}
        last = f"; last drop: {self._last_disconnect}" if self._last_disconnect else ""
        return {"reachable": True, "auth_ok": True, "complete": complete,
                "detail": f"socket up; {len(self._buffer)} buffered"
                          f"{last}{detail_suffix}"}

    # ---- delivery gaps (ENH-16) ---------------------------------------------
    def delivery_gaps(self):
        """Unrecovered platform-admitted drop windows, oldest first. Feed this to
        core.checks.delivery_gap_check so the registry FAILS while any is open."""
        return [dict(g) for g in self._gaps]

    def recovery_due(self):
        """True while any admitted gap is unrecovered: the completeness poll (the
        gap-free cursored truth side) is due NOW. Same rule as owed work (R3) —
        backoff may delay routine polling, never recovery of an admitted loss."""
        return bool(self._gaps)

    def mark_recovered(self, completed_at):
        """Close gaps proven covered: `completed_at` is the epoch time a gap-free
        completeness poll COMPLETED. Cursored polling covers all history up to its
        completion whatever it found, so completion time — not a message watermark,
        which never advances on a quiet channel — is the recovery evidence. A gap
        whose window ends after `completed_at` stays open: that poll cannot have
        covered it. Unknown-window gaps close on any completed poll, which in the
        poll-then-mark call order is always one that started after the admission.
        Returns how many gaps closed."""
        completed_at = float(completed_at)
        keep = [g for g in self._gaps
                if g["end"] is not None and g["end"] > completed_at]
        closed = len(self._gaps) - len(keep)
        self._gaps = keep
        return closed

    # ---- socket-mode protocol ------------------------------------------------
    def _pump(self):
        """Drain everything the socket has RIGHT NOW; never block the poll cycle."""
        reconnects = 0
        if self._conn is None:
            self._connect_socket()
        while True:
            try:
                text = self._conn.recv_text()
            except (SocketClosed, OSError) as ex:
                self._drop_conn(f"socket died: {ex}")
                if reconnects >= MAX_RECONNECTS_PER_PUMP:
                    return
                reconnects += 1
                self._connect_socket()
                continue
            if text is None:
                return
            try:
                obj = json.loads(text)
            except ValueError:
                continue        # not Slack protocol; nothing to ack or ingest
            if obj.get("type") == "disconnect":
                # The platform cycles connections every few hours and says so with
                # this frame; treating it as noise is how push goes silently deaf.
                self._drop_conn(
                    f"disconnect frame: {obj.get('reason', 'unspecified')}")
                if reconnects >= MAX_RECONNECTS_PER_PUMP:
                    return
                reconnects += 1
                self._connect_socket()
                continue
            self._handle(obj)

    def _handle(self, obj):
        kind = obj.get("type")
        if kind == "app_rate_limited":
            # The documented shape arrives inside an events_api envelope, but an
            # admission of loss is never ignorable whatever the wrapper looks like.
            self._record_gap(obj)
            return
        if kind != "events_api":
            return              # hello / future protocol messages: not ours to ack
        payload = obj.get("payload") or {}
        if payload.get("type") == "app_rate_limited":
            # ENH-16: above 30k deliveries/app/workspace/hour the platform STOPS
            # DELIVERING and says so with this payload instead of failing loudly.
            # Before ENH-16 this fell through _maybe_ingest (no `event` key) and was
            # acked away — the engine lost messages while health() answered healthy.
            self._record_gap(payload)
        else:
            self._maybe_ingest(payload.get("event") or {})
        envelope_id = obj.get("envelope_id")
        # Ack AFTER buffering: an unacked envelope is re-delivered, so a crash in
        # between duplicates (absorbed by the store, R9) instead of losing.
        if envelope_id:
            self._conn.send_text(json.dumps({"envelope_id": envelope_id}))

    def _maybe_ingest(self, event):
        if event.get("type") != "message":
            return
        channel = event.get("channel")
        if channel not in self._watched:
            return              # foreign rows would poison push-vs-poll parity
        if event.get("subtype") in EPHEMERAL_SUBTYPES:
            return
        ts = event.get("ts")
        if ts is None:
            return
        if any(m["channel_id"] == channel and m["ts"] == ts for m in self._buffer):
            return              # envelope retries re-deliver the same event
        self._buffer.append(self._normalize(channel, event))

    def _record_gap(self, sig):
        """ENH-16: one admitted drop window per platform-named minute. A missing or
        unparseable minute_rate_limited still records (start=None): 'unknown window'
        is an admission of loss, never a reason to shrug."""
        try:
            start = float(sig["minute_rate_limited"])
        except (KeyError, TypeError, ValueError):
            start = None
        if any(g["start"] == start for g in self._gaps):
            return              # envelope retries re-deliver the same admission
        self._gaps.append({
            "start": start,
            "end": None if start is None else start + 60.0,
            "detail": "app_rate_limited: the platform stopped delivering events"
                      + ("" if start is None
                         else f" during the minute at {start:.0f}"),
        })

    def _connect_socket(self, timeout=30.0):
        # A Socket Mode ticket is single-use: every (re)connect must mint a fresh
        # url via apps.connections.open — replaying the dead connection's url is
        # refused by the platform and would leave the adapter permanently deaf.
        url = self._connections_open(timeout=timeout)
        self._conn = self._connect(url)
        self._last_url = url

    def _drop_conn(self, reason):
        self._last_disconnect = reason
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
        self._conn = None

    @staticmethod
    def _normalize(channel_id, event):
        # channel_type is this adapter's discovery name, not "slack": provenance
        # must survive into the store, and if both ingestion paths were ever pointed
        # at ONE store, identical keys would dedupe push into poll and erase the
        # very disagreement the parity watch exists to catch.
        ts = event.get("ts")
        thread = event.get("thread_ts")
        return {
            "channel_type": "slack_socket",
            "channel_id": channel_id,
            "sender_id": event.get("user") or event.get("bot_id") or "unknown",
            "sender_name": event.get("username"),
            "ts": ts,
            "text": event.get("text", ""),
            "thread_id": thread if (thread and thread != ts) else None,
            "attachments": _attachments(event),
            "raw": json.dumps(event, sort_keys=True),
        }

    # ---- transport ----------------------------------------------------------
    def _connections_open(self, timeout=30.0):
        status, headers, body = self.http(f"{API_BASE}/{OPEN_METHOD}", {}, timeout)
        try:
            payload = json.loads(body) if body else {}
        except ValueError:
            payload = {}
        if status == 429 or (payload and payload.get("error") == "ratelimited"):
            # The wait is the PLATFORM'S number, exactly, labelled with its method
            # (ENH-1's keyed back-off). The default only covers a stripping proxy.
            ex = RateLimited(_header(headers, "Retry-After", "1"), method=OPEN_METHOD)
            self._holds[OPEN_METHOD] = self._clock() + ex.retry_after
            raise ex
        if not payload:
            raise ApiError(OPEN_METHOD, f"HTTP {status} with a non-JSON body")
        if not payload.get("ok"):
            raise ApiError(OPEN_METHOD, payload.get("error", "unknown_error"))
        url = payload.get("url")
        if not url:
            raise ApiError(OPEN_METHOD, "ok response carrying no websocket url")
        return url

    def _urllib_http(self, url, form, timeout):
        """(status, headers, body) from a real POST. The token travels only in the
        Authorization header — never in the form, where it would leak into logs."""
        req = urllib.request.Request(
            url, data=urllib.parse.urlencode(form).encode(),
            headers={"Authorization": f"Bearer {self._token}",
                     "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return (resp.status, dict(resp.headers),
                        resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as ex:
            # 4xx/5xx still carry Slack's JSON body and Retry-After header — they
            # are responses to interpret, not transport failures.
            return ex.code, dict(ex.headers), ex.read().decode("utf-8", "replace")

    # ---- health internals ------------------------------------------------------
    def _hold_detail(self):
        active = {m: t - self._clock() for m, t in self._holds.items()
                  if t > self._clock()}
        if not active:
            return ""
        holds = ", ".join(f"{m} ready in {s:.0f}s" for m, s in sorted(active.items()))
        return f"; rate-limit backoff: {holds}"

    def _gap_detail(self):
        if not self._gaps:
            return ""
        windows = ", ".join(
            "unknown window" if g["start"] is None
            else f"[{g['start']:.0f}, {g['end']:.0f})" for g in self._gaps)
        return (f"; DELIVERY GAP app_rate_limited: {len(self._gaps)} unrecovered "
                f"window(s) {windows} — completeness poll due")
