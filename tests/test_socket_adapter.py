"""Socket Mode ingestion adapter tests (ENH-14; push for latency, poll for truth).

The adapter under test is the second ingestion path for the same platform the polling
`slack` adapter reads. Socket Mode delivers Events API payloads over an OUTBOUND
WebSocket — no public endpoint, no inbound firewall hole — which fits the loopback-only
constraint that forced the incumbent into polling. But the platform documents that
events can be MISSED (10-connection cap, connections cycle every few hours), so this
adapter is never the truth: scripts/push-poll-parity.py diffs it against the polling
store continuously, and these tests pin the properties that keep that diff honest:

* reconnect on disconnect frames — THE acceptance property: the platform cycles
  connections and says so with a disconnect frame; ignoring it makes push silently deaf;
* every reconnect mints a FRESH url via apps.connections.open (tickets are single-use);
* every events_api envelope is ACKed with its envelope_id — unacked envelopes are
  re-delivered and then dropped by the platform;
* only watched channels are ingested, and ephemeral edit wrappers (message_changed,
  message_deleted) are skipped — either would poison push-vs-poll parity permanently;
* re-polling with the same cursor may duplicate, never lose (channels/CONTRACT.md);
* no send surface exists at any layer (ingestion-only, like the polling slack adapter);
* health can FAIL (contract rule 5).

All network I/O is faked (scripted HTTP + scripted websocket connections); the RFC 6455
frame layer is tested directly against hand-built byte sequences. Nothing here touches
Slack.
"""
import importlib.util
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import discover_adapters, load_adapter_class  # noqa: E402
from core.ratelimit import RateLimited  # noqa: E402
from core.store import Store  # noqa: E402

ADAPTER_PY = ROOT / "channels" / "slack_socket" / "adapter.py"


def _load_module():
    """The module itself (not just the Adapter class) — the tests pin the websocket
    frame layer and its exceptions, which load_adapter_class deliberately hides."""
    spec = importlib.util.spec_from_file_location("socket_adapter_under_test", ADAPTER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeHttp:
    """Scripted transport for apps.connections.open. Records every wire call so the
    fresh-url tests can count how many tickets were minted."""

    def __init__(self, script=()):
        self.script = list(script)
        self.requests = []  # (url, form) per wire call

    def __call__(self, url, form, timeout):
        self.requests.append((url, dict(form)))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        status, headers, body = step
        if isinstance(body, dict):
            body = json.dumps(body)
        return status, headers, body


class FakeConn:
    """Scripted websocket connection: serves text payloads in order (dicts are JSON-
    encoded), raises scripted exceptions, records everything sent back (the acks)."""

    def __init__(self, frames=()):
        self.frames = list(frames)
        self.sent = []          # parsed JSON of every send_text
        self.closed = False

    def recv_text(self):
        if not self.frames:
            return None
        item = self.frames.pop(0)
        if isinstance(item, Exception):
            raise item
        return json.dumps(item) if isinstance(item, dict) else item

    def send_text(self, text):
        self.sent.append(json.loads(text))

    def close(self):
        self.closed = True


class FakeConnect:
    """connect(url) factory that hands out scripted connections and records the urls,
    so tests can prove every reconnect used a FRESH single-use ticket."""

    def __init__(self, conns=()):
        self.conns = list(conns)
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        return self.conns.pop(0)


def open_ok(url="wss://sock.example/link"):
    return (200, {}, {"ok": True, "url": url})


def message_event(ts, channel="C_ONE", user="U_A", text="hello", **extra):
    ev = {"type": "message", "channel": channel, "user": user, "text": text, "ts": ts}
    ev.update(extra)
    return ev


def envelope(envelope_id, event):
    return {"type": "events_api", "envelope_id": envelope_id,
            "payload": {"event": event}}


DISCONNECT = {"type": "disconnect", "reason": "refresh_requested"}
HELLO = {"type": "hello", "num_connections": 1}


class SocketAdapterTestCase(unittest.TestCase):
    def setUp(self):
        self.mod = _load_module()

    def make(self, http_script=(), conns=(), channels="C_ONE", clock=None):
        self.http = FakeHttp(http_script)
        self.connect = FakeConnect(conns)
        kwargs = {"clock": clock} if clock else {}
        return self.mod.Adapter(auth={"app_token": "app-tok", "channels": channels},
                                http=self.http, connect=self.connect, **kwargs)


class ContractSurfaceTest(SocketAdapterTestCase):
    def test_slack_socket_is_discovered_from_the_shipped_tree(self):
        """R11 discovery is the landing mechanism — a dir-drop, zero core/ changes."""
        self.assertIn("slack_socket", discover_adapters(ROOT / "channels"))
        cls = load_adapter_class(ROOT / "channels", "slack_socket")
        self.assertTrue(callable(cls))

    def test_capabilities_answer_every_contract_key(self):
        caps = self.make().capabilities()
        for k in ("read", "history", "search", "send", "react", "threads"):
            self.assertIn(k, caps)
        self.assertTrue(caps["read"])
        self.assertFalse(caps["history"],
                         "Socket Mode delivers live events only — claiming history "
                         "would tell the engine backfill is covered when it is not")
        self.assertFalse(caps["send"],
                         "an ingestion-only adapter advertising send invites the "
                         "outbox to drive a path that must not exist")

    def test_no_callable_send_exists_at_any_public_surface(self):
        a = self.make()
        self.assertIsNone(getattr(a, "send", None))
        self.assertIsNone(getattr(a, "read_back", None),
                          "read_back is proof-of-delivery; it implies a delivery path")
        for name in dir(a):
            if name.startswith("_"):
                continue
            for banned in ("send", "post", "write", "publish", "react"):
                self.assertNotIn(banned, name.lower(),
                                 f"public attribute {name!r} looks like a send surface")

    def test_missing_app_token_or_channels_refuses_at_construction(self):
        with self.assertRaises(ValueError) as ctx:
            self.mod.Adapter(auth={"channels": "C_ONE"})
        self.assertIn("app_token", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            self.mod.Adapter(auth={"app_token": "app-tok"})
        self.assertIn("channels", str(ctx.exception))

    def test_resolve_passes_ids_through_and_refuses_names(self):
        """An app-level token cannot call the Web API directory; pretending to resolve
        a name would silently hand back garbage. Ids pass through — the id IS the id."""
        a = self.make()
        self.assertEqual(a.resolve("C12345678"), "C12345678")
        self.assertEqual(a.resolve("U12345678"), "U12345678")
        with self.assertRaises(LookupError) as ctx:
            a.resolve("#team-room")
        self.assertIn("slack", str(ctx.exception).lower())


class IngestTest(SocketAdapterTestCase):
    def test_pushed_events_normalize_to_the_pinned_contract_and_the_store_accepts_them(self):
        parent = message_event("3.0", thread_ts="3.0")   # thread PARENT: thread_ts == ts
        reply = message_event("4.0", thread_ts="3.0")
        conn = FakeConn([HELLO, envelope("e1", reply), envelope("e2", parent),
                         envelope("e3", message_event("2.0"))])
        a = self.make([open_ok()], [conn])
        msgs, cursor = a.poll(None)

        self.assertEqual([m["ts"] for m in msgs], ["2.0", "3.0", "4.0"],
                         "events arrive in delivery order; the engine contract is "
                         "sortable oldest-first")
        for m in msgs:
            self.assertEqual(m["channel_type"], "slack_socket")
            self.assertEqual(m["channel_id"], "C_ONE")
            self.assertIsInstance(m["raw"], str,
                                  "raw must be a JSON string — the store binds it "
                                  "straight into sqlite")
        by_ts = {m["ts"]: m for m in msgs}
        self.assertIsNone(by_ts["3.0"]["thread_id"])
        self.assertEqual(by_ts["4.0"]["thread_id"], "3.0")

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "m.db")
            try:
                store.upsert_messages(msgs)
                self.assertEqual(store.count("C_ONE"), 3)
            finally:
                store.close()
        self.assertEqual(json.loads(cursor), {"C_ONE": "4.0"})

    def test_every_envelope_is_acked_with_its_envelope_id(self):
        """Unacked envelopes are re-delivered and then DROPPED by the platform — the
        ack is what keeps push delivery alive. Everything is acked, ingested or not."""
        conn = FakeConn([envelope("env-1", message_event("1.0")),
                         envelope("env-2", message_event("9.9", channel="C_OTHER"))])
        a = self.make([open_ok()], [conn])
        a.poll(None)
        self.assertEqual(conn.sent, [{"envelope_id": "env-1"},
                                     {"envelope_id": "env-2"}])

    def test_events_for_unwatched_channels_are_acked_but_never_ingested(self):
        """Foreign rows would poison push-vs-poll parity: the differ compares exactly
        the watched channels, so an unwatched ingest shows up as permanent 'extra'."""
        conn = FakeConn([envelope("e1", message_event("1.0", channel="C_OTHER")),
                         envelope("e2", message_event("2.0"))])
        a = self.make([open_ok()], [conn])
        msgs, _ = a.poll(None)
        self.assertEqual([m["channel_id"] for m in msgs], ["C_ONE"])
        self.assertEqual(len(conn.sent), 2, "the unwatched envelope was not acked")

    def test_ephemeral_edit_wrappers_are_not_ingested_as_new_rows(self):
        """message_changed/message_deleted are event-only wrappers that never appear
        as rows in conversations.history — ingesting one creates a row the poll side
        can never confirm: a PERMANENT parity divergence."""
        changed = {"type": "message", "subtype": "message_changed", "channel": "C_ONE",
                   "ts": "5.1", "message": {"ts": "5.0", "text": "edited"}}
        deleted = {"type": "message", "subtype": "message_deleted", "channel": "C_ONE",
                   "ts": "6.1", "deleted_ts": "6.0"}
        conn = FakeConn([envelope("e1", changed), envelope("e2", deleted),
                         envelope("e3", message_event("7.0"))])
        a = self.make([open_ok()], [conn])
        msgs, _ = a.poll(None)
        self.assertEqual([m["ts"] for m in msgs], ["7.0"])
        self.assertEqual(len(conn.sent), 3, "ephemeral wrappers must still be acked")

    def test_non_message_events_and_junk_frames_are_tolerated(self):
        conn = FakeConn(["this is not json",
                         envelope("e1", {"type": "reaction_added", "user": "U_A"}),
                         envelope("e2", message_event("1.0"))])
        a = self.make([open_ok()], [conn])
        msgs, _ = a.poll(None)
        self.assertEqual([m["ts"] for m in msgs], ["1.0"])

    def test_a_senderless_event_still_meets_the_required_fields(self):
        """The store REQUIRES sender_id (R5). Bot/system events carry bot_id or
        nothing; the adapter must map them to something non-None, never drop the row."""
        bot = {"type": "message", "channel": "C_ONE", "ts": "6.0",
               "text": "from a bot", "bot_id": "B_X"}
        a = self.make([open_ok()], [FakeConn([envelope("e1", bot)])])
        msgs, _ = a.poll(None)
        self.assertEqual(msgs[0]["sender_id"], "B_X")
        Store.validate(msgs[0])


class AttachmentIngestTest(SocketAdapterTestCase):
    """ENH-4, push side: the same message event carries the same `files` array Socket
    Mode or history. Both ingestion paths must keep the upload, or push-vs-poll parity
    would hold on ts while the two stores disagree about what the message contained."""

    FILE = {"id": "F_X", "name": "screenshot.png", "title": "screenshot",
            "mimetype": "image/png", "url_private": "https://files.example/x.png"}

    def test_an_upload_carrying_event_keeps_its_attachment(self):
        ev = message_event("1.0", text="", files=[self.FILE])
        a = self.make([open_ok()], [FakeConn([envelope("e1", ev)])])
        msgs, _ = a.poll(None)
        atts = msgs[0]["attachments"]
        self.assertEqual(len(atts), 1)
        self.assertEqual(atts[0], {"kind": "image", "name": "screenshot.png",
                                   "mimetype": "image/png",
                                   "url": "https://files.example/x.png"})
        Store.validate(msgs[0])

    def test_a_fileless_event_carries_a_known_empty_list(self):
        a = self.make([open_ok()], [FakeConn([envelope("e1", message_event("2.0"))])])
        msgs, _ = a.poll(None)
        self.assertEqual(msgs[0]["attachments"], [])


class CursorTest(SocketAdapterTestCase):
    def test_repolling_the_same_cursor_returns_the_same_messages_never_fewer(self):
        """channels/CONTRACT.md: a re-poll may DUPLICATE, never lose. The buffer may
        only be pruned at the cursor the ENGINE hands back — that is the one durable
        acknowledgement that a message will never be asked for again."""
        conn = FakeConn([envelope("e1", message_event("1.0")),
                         envelope("e2", message_event("2.0"))])
        a = self.make([open_ok()], [conn])
        first, cursor = a.poll(None)
        again, cursor2 = a.poll(None)
        self.assertEqual(first, again)
        self.assertEqual(cursor, cursor2)
        after, cursor3 = a.poll(cursor)
        self.assertEqual(after, [])
        self.assertEqual(cursor3, cursor)

    def test_an_empty_poll_returns_the_cursor_unchanged(self):
        a = self.make([open_ok()], [FakeConn([])])
        msgs, cursor = a.poll('{"C_ONE": "5.0"}')
        self.assertEqual(msgs, [])
        self.assertEqual(cursor, '{"C_ONE": "5.0"}')

    def test_a_junk_cursor_is_refused_not_guessed_at(self):
        a = self.make()
        with self.assertRaises(ValueError):
            a.poll("not-json")
        with self.assertRaises(ValueError):
            a.poll('"a-string-not-an-object"')


class ReconnectTest(SocketAdapterTestCase):
    """THE acceptance property (ENH-14): the platform cycles connections every few
    hours and announces it with a disconnect frame. An adapter that shrugs at that
    frame keeps a dead socket and goes silently deaf — the exact failure class this
    project exists to eliminate."""

    def test_a_disconnect_frame_triggers_reconnect_on_a_fresh_url(self):
        conn1 = FakeConn([envelope("e1", message_event("1.0")), DISCONNECT])
        conn2 = FakeConn([envelope("e2", message_event("2.0"))])
        a = self.make([open_ok("wss://sock.example/one"),
                       open_ok("wss://sock.example/two")], [conn1, conn2])
        msgs, _ = a.poll(None)

        self.assertEqual([m["ts"] for m in msgs], ["1.0", "2.0"],
                         "events buffered before the disconnect were lost, or events "
                         "from the new connection never arrived")
        # Tickets are single-use: the reconnect must mint a FRESH url via a second
        # apps.connections.open call, never replay the dead connection's url.
        self.assertEqual(self.connect.urls,
                         ["wss://sock.example/one", "wss://sock.example/two"])
        self.assertEqual(len(self.http.requests), 2,
                         "reconnect did not go back to apps.connections.open")
        self.assertTrue(conn1.closed, "the dead connection was left open")

    def test_a_dead_socket_triggers_reconnect_too(self):
        conn1 = FakeConn([self.mod.SocketClosed("connection closed by peer (EOF)")])
        conn2 = FakeConn([envelope("e1", message_event("2.0"))])
        a = self.make([open_ok("wss://sock.example/one"),
                       open_ok("wss://sock.example/two")], [conn1, conn2])
        msgs, _ = a.poll(None)
        self.assertEqual([m["ts"] for m in msgs], ["2.0"])
        self.assertEqual(len(self.connect.urls), 2)

    def test_a_reconnect_storm_is_bounded_per_poll(self):
        """A flapping server must not trap one poll() call in an infinite reconnect
        loop — the cycle returns what it has and health carries the last reason."""
        n = 1 + self.mod.MAX_RECONNECTS_PER_PUMP
        conns = [FakeConn([DISCONNECT]) for _ in range(n + 2)]
        a = self.make([open_ok(f"wss://sock.example/{i}") for i in range(n + 2)], conns)
        msgs, _ = a.poll(None)
        self.assertEqual(msgs, [])
        self.assertEqual(len(self.connect.urls), n,
                         "reconnects within one poll() must be bounded")


class RateLimitAndHealthTest(SocketAdapterTestCase):
    def test_429_on_connections_open_surfaces_the_exact_retry_after_and_method(self):
        """ENH-1's back-off is keyed (instance, method); a 429 that loses either the
        platform's number or the method identity collapses that scope."""
        a = self.make([(429, {"Retry-After": "17"},
                        {"ok": False, "error": "ratelimited"})])
        with self.assertRaises(RateLimited) as ctx:
            a.poll(None)
        self.assertEqual(ctx.exception.retry_after, 17.0)
        self.assertEqual(ctx.exception.method, "apps.connections.open")

    def test_health_passes_when_the_socket_is_up(self):
        a = self.make([open_ok()], [FakeConn([])])
        h = a.health()
        self.assertTrue(h["reachable"])
        self.assertTrue(h["auth_ok"])

    def test_health_fails_on_bad_app_token(self):
        """Contract rule 5: a health check that can only pass is a defect."""
        a = self.make([(200, {}, {"ok": False, "error": "invalid_auth"})])
        h = a.health()
        self.assertTrue(h["reachable"])
        self.assertFalse(h["auth_ok"])
        self.assertIn("invalid_auth", h["detail"])

    def test_health_fails_when_the_platform_is_unreachable(self):
        a = self.make([OSError("no route to host")])
        h = a.health()
        self.assertFalse(h["reachable"])
        self.assertFalse(h["auth_ok"])


# ---------------------------------------------------------------------------
# RFC 6455 frame layer — tested against hand-built byte sequences.
# ---------------------------------------------------------------------------

class FakeSock:
    def __init__(self, chunks=()):
        self.chunks = list(chunks)
        self.sent = b""
        self.closed = False

    def sendall(self, data):
        self.sent += data

    def recv(self, n):
        if not self.chunks:
            raise socket.timeout()
        item = self.chunks.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def settimeout(self, t):
        pass

    def close(self):
        self.closed = True


KEY = "dGhlIHNhbXBsZSBub25jZQ=="        # the RFC 6455 §1.3 example nonce


class WebSocketFrameTest(unittest.TestCase):
    def setUp(self):
        self.mod = _load_module()

    def handshake(self, accept=None):
        accept = accept if accept is not None else self.mod._accept_for(KEY)
        return ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"
                ).encode()

    def connect(self, chunks):
        self.sock = FakeSock(chunks)
        return self.mod._WebSocketConnection("wss://sock.example/link?ticket=t",
                                             sock=self.sock, key=KEY)

    def server_text(self, payload):
        return self.mod._encode_frame(self.mod.OP_TEXT, payload, None)

    def client_frames_sent(self):
        """Parse the client frames that followed the handshake request."""
        _, _, rest = self.sock.sent.partition(b"\r\n\r\n")
        frames = []
        while rest:
            fin, op, payload, used = self.mod._parse_frame(rest)
            frames.append((op, payload))
            rest = rest[used:]
        return frames

    def test_accept_key_matches_the_rfc6455_worked_example(self):
        self.assertEqual(self.mod._accept_for(KEY), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_a_wrong_accept_key_refuses_the_upgrade(self):
        """A wrong accept means we are not speaking RFC 6455 to who we think — parsing
        frames out of an impostor stream would be worse than failing."""
        with self.assertRaises(self.mod.SocketClosed):
            self.connect([self.handshake(accept="bm90LXRoZS1yaWdodC1rZXk=")])

    def test_masked_roundtrip_and_extended_lengths(self):
        mask = b"\x01\x02\x03\x04"
        for payload in (b"hi", b"x" * 200, b"y" * 70000):
            frame = self.mod._encode_frame(self.mod.OP_TEXT, payload, mask)
            fin, op, out, used = self.mod._parse_frame(frame)
            self.assertTrue(fin)
            self.assertEqual(op, self.mod.OP_TEXT)
            self.assertEqual(out, payload)
            self.assertEqual(used, len(frame))
        self.assertIsNone(self.mod._parse_frame(b"\x81"),
                          "an incomplete frame must parse as 'wait for more bytes'")

    def test_client_frames_are_masked_on_the_wire(self):
        """RFC 6455 §5.3: a server MUST drop the connection on an unmasked client
        frame — removing masking kills the link in production, invisibly in tests."""
        conn = self.connect([self.handshake()])
        conn.send_text("payload-visible-if-unmasked")
        _, _, raw = self.sock.sent.partition(b"\r\n\r\n")
        self.assertTrue(raw[1] & 0x80, "the mask bit is not set on a client frame")
        self.assertNotIn(b"payload-visible-if-unmasked", raw)

    def test_recv_text_returns_frames_in_order_then_none_when_idle(self):
        conn = self.connect([self.handshake(),
                             self.server_text(b'{"a":1}') + self.server_text(b'{"b":2}')])
        self.assertEqual(conn.recv_text(), '{"a":1}')
        self.assertEqual(conn.recv_text(), '{"b":2}')
        self.assertIsNone(conn.recv_text())

    def test_a_ping_is_answered_with_a_pong_carrying_the_same_payload(self):
        conn = self.connect([self.handshake(),
                             self.mod._encode_frame(self.mod.OP_PING, b"beat", None)])
        self.assertIsNone(conn.recv_text())
        self.assertIn((self.mod.OP_PONG, b"beat"), self.client_frames_sent())

    def test_fragmented_text_is_reassembled(self):
        first = self.mod._encode_frame(self.mod.OP_TEXT, b'{"half":', None, fin=False)
        second = self.mod._encode_frame(self.mod.OP_CONT, b'"done"}', None)
        conn = self.connect([self.handshake(), first + second])
        self.assertEqual(conn.recv_text(), '{"half":"done"}')

    def test_texts_before_a_close_frame_are_delivered_before_the_close_raises(self):
        """A close frame ends what FOLLOWS it; raising early would lose parsed events
        that arrived in the same read — a lost message, the one unforgivable thing."""
        conn = self.connect([self.handshake(),
                             self.server_text(b'{"last":1}')
                             + self.mod._encode_frame(self.mod.OP_CLOSE, b"", None)])
        self.assertEqual(conn.recv_text(), '{"last":1}')
        with self.assertRaises(self.mod.SocketClosed):
            conn.recv_text()

    def test_eof_raises_socket_closed(self):
        conn = self.connect([self.handshake(), b""])
        with self.assertRaises(self.mod.SocketClosed):
            conn.recv_text()


if __name__ == "__main__":
    unittest.main(verbosity=2)
