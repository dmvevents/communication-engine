"""Telegram read-only adapter tests (ENH-25; the first NON-Slack platform).

R11's "a new channel type is a directory drop" was proven by a fake adapter and two
Slack adapters that share a cursor model, a rate-limit dialect and an id shape. Telegram
disagrees with Slack about the most important thing in this engine: whether re-reading
is possible at all. Bots have NO history API — `getUpdates` is a queue whose `offset`
parameter permanently ACKNOWLEDGES everything below it — so the properties pinned here
are the ones that keep "never lose messages" true on a platform that destroys the
window as you advance:

* the offset sent to the platform is EXACTLY the cursor the engine handed back — the
  one durable acknowledgement (committed only after the store/journal write,
  core/schedule.py) — never a high-water mark the adapter remembered for itself;
* poll() makes exactly ONE destructive read per call: in-poll pagination would advance
  the offset past messages the engine has not committed, and a crash mid-drain loses
  them at the platform, unrecoverably;
* one instance watches exactly ONE chat: the engine keys cursors per (instance,
  channel) but the bot queue has a single ack authority, so a second chat sharing it
  could have its un-journaled messages acknowledged by the first chat's committed
  cursor (a crash between the two per-channel commits in one scheduler cycle);
* `ts` is the message_id, not message.date: the store keys rows (channel_type,
  channel_id, ts) and date is whole seconds — two messages in one second would merge;
* a 429's wait lives in the JSON body (`parameters.retry_after`), not the Retry-After
  header — a helper that only reads headers would silently invent a wait;
* `retrievable_ts` is deliberately ABSENT and must stay absent: a bot cannot ask for
  what it has acknowledged, so parity is permanently fail-closed and core/parity's
  ENH-27 declaration is the honest explanation — a stub snapshot would mark every
  still-real message "deleted upstream", the one direction that hides a real loss;
* no send path at any layer, same as channels/slack — and the live HMAC bridge on the
  incumbent host is explicitly out of scope.

All network I/O is faked via the injectable transport; nothing here touches Telegram.
"""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import discover_adapters, load_adapter_class  # noqa: E402
from core.parity import snapshot_declaration  # noqa: E402
from core.ratelimit import RateLimited  # noqa: E402
from core.store import Store  # noqa: E402

ADAPTER_PY = ROOT / "channels" / "telegram" / "adapter.py"


def _load_module():
    """The module itself (not just the Adapter class) — the tests pin the transport
    guard exceptions, which load_adapter_class deliberately does not expose."""
    spec = importlib.util.spec_from_file_location("telegram_adapter_under_test",
                                                  ADAPTER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeHttp:
    """Scripted transport. Consumes responses in order and records every request that
    actually reached the wire — the no-send tests assert on requests staying EMPTY."""

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


def tg_message(message_id, chat="12345", date=1700000000, text="hello", **extra):
    """A raw Telegram Message object, as the platform sends it."""
    m = {"message_id": message_id, "date": date,
         "chat": {"id": int(chat), "type": "private"},
         "from": {"id": 777, "is_bot": False, "first_name": "Anton"}}
    if text is not None:
        m["text"] = text
    m.update(extra)
    return m


def tg_update(update_id, message, kind="message"):
    return {"update_id": update_id, kind: message}


def updates(*ups):
    return (200, {}, {"ok": True, "result": list(ups)})


class TelegramAdapterTestCase(unittest.TestCase):
    def setUp(self):
        self.mod = _load_module()

    def make(self, script=(), channels="12345", clock=None):
        self.http = FakeHttp(script)
        kwargs = {"clock": clock} if clock else {}
        return self.mod.Adapter(auth={"token": "tok", "channels": channels},
                                http=self.http, **kwargs)


class ContractSurfaceTest(TelegramAdapterTestCase):
    def test_telegram_is_discovered_from_the_shipped_tree(self):
        """R11 discovery is the landing mechanism — a dir-drop, zero core/ changes."""
        self.assertIn("telegram", discover_adapters(ROOT / "channels"))
        cls = load_adapter_class(ROOT / "channels", "telegram")
        self.assertTrue(callable(cls))

    def test_capabilities_answer_every_contract_key_and_are_honest(self):
        caps = self.make().capabilities()
        for k in ("read", "history", "search", "send", "react", "threads"):
            self.assertIn(k, caps)
        self.assertTrue(caps["read"])
        self.assertFalse(caps["send"],
                         "a read-only adapter advertising send invites the outbox to "
                         "drive a path that must not exist")
        self.assertFalse(caps["history"],
                         "Telegram bots have no history API — claiming history invites "
                         "core to plan re-reads the platform cannot serve")

    def test_no_callable_send_exists_at_any_public_surface(self):
        """Same acceptance property as channels/slack: nothing named like a send for
        any caller — outbox included — to find. The live bridge is out of scope."""
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

    def test_missing_token_or_channels_refuses_at_construction(self):
        with self.assertRaises(ValueError) as ctx:
            self.mod.Adapter(auth={"channels": "12345"})
        self.assertIn("token", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            self.mod.Adapter(auth={"token": "tok"})
        self.assertIn("channels", str(ctx.exception))

    def test_a_second_chat_is_refused_naming_the_reason(self):
        """THE structural consequence of the destructive ack: the engine keys cursors
        per (instance, channel), but one bot queue has ONE ack authority. Two chats
        sharing it means chat A's committed cursor can acknowledge chat B's messages
        before B's journal write (crash between the two per-channel commits in a
        scheduler cycle) — a silent, unrecoverable loss. One bot per chat instead."""
        with self.assertRaises(ValueError) as ctx:
            self.make(channels="12345,67890")
        msg = str(ctx.exception).lower()
        self.assertIn("one chat", msg)
        self.assertIn("cursor", msg,
                      "the refusal must explain the cursor/ack conflict, or the next "
                      "author reads it as an arbitrary cap and lifts it")

    def test_a_non_numeric_chat_id_is_refused(self):
        """getUpdates rows carry numeric chat ids and the engine matches channel_id
        strings exactly — '@name' here would poll successfully and match nothing,
        the silent watch-nothing failure (the two-place lesson, docs/QUICKSTART.md)."""
        with self.assertRaises(ValueError) as ctx:
            self.make(channels="@myclub")
        self.assertIn("numeric", str(ctx.exception))

    def test_the_watch_set_is_exposed_for_the_doctor_cross_check(self):
        self.assertEqual(self.make().channels, ("12345",))

    def test_a_negative_group_chat_id_is_accepted_and_survives(self):
        """Group ids are negative — a leading '-' has bitten id-shaped regexes before
        (channels/slack uses [CDG][A-Z0-9]{6,}; Telegram needs its own shape)."""
        a = self.make([updates(tg_update(7, tg_message(1, chat="-100987")))],
                      channels="-100987")
        msgs, _ = a.poll(None)
        self.assertEqual(msgs[0]["channel_id"], "-100987")


class ReadOnlyTransportTest(TelegramAdapterTestCase):
    """Deny-by-default at the layer every request must pass through. Without this, the
    no-send property above is one convenience wrapper away from being lost."""

    WRITE_METHODS = ("sendMessage", "sendPhoto", "sendDocument", "editMessageText",
                     "deleteMessage", "banChatMember", "setWebhook",
                     "answerCallbackQuery", "leaveChat")

    def test_write_methods_are_refused_before_any_io(self):
        a = self.make()
        for method in self.WRITE_METHODS:
            with self.assertRaises(self.mod.ReadOnlyViolation, msg=method):
                a._api(method, chat_id="12345", text="never")
        self.assertEqual(self.http.requests, [],
                         "a refused method still reached the wire — the guard runs "
                         "after I/O, which is no guard at all")

    def test_read_methods_pass_the_guard(self):
        a = self.make([(200, {}, {"ok": True, "result": {}})])
        a._api("getMe")
        self.assertEqual(len(self.http.requests), 1)


class PollTest(TelegramAdapterTestCase):
    def test_poll_normalizes_to_the_pinned_contract_and_the_store_accepts_it(self):
        a = self.make([updates(
            tg_update(100, tg_message(11, text="first")),
            tg_update(101, tg_message(12, text="second",
                                      reply_to_message={"message_id": 11})),
        )])
        msgs, cursor = a.poll(None)
        self.assertEqual([m["ts"] for m in msgs], ["11", "12"])
        for m in msgs:
            self.assertEqual(m["channel_type"], "telegram")
            self.assertEqual(m["channel_id"], "12345")
            self.assertEqual(m["sender_id"], "777")
            self.assertIsInstance(m["raw"], str,
                                  "raw must be a JSON string — the store binds it "
                                  "straight into sqlite")
        # Reply-chain threading: thread_id is the parent's message_id, which IS the
        # parent's ts — the same alignment Slack's thread_ts gives core for free.
        self.assertIsNone(msgs[0]["thread_id"])
        self.assertEqual(msgs[1]["thread_id"], "11")

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "m.db")
            try:
                store.upsert_messages(msgs)
                self.assertEqual(store.count("12345"), 2)
            finally:
                store.close()
        self.assertEqual(cursor, "102",
                         "the cursor must be last update_id + 1 — the getUpdates "
                         "resume convention")

    def test_the_offset_sent_is_exactly_the_engine_committed_cursor(self):
        """THE gap-free property. offset=N permanently acknowledges every update
        below N, so the only safe N is the cursor the engine hands back — committed
        strictly after the store/journal write (core/schedule.py). An adapter that
        remembers its own high-water mark acknowledges messages the engine has not
        yet made durable."""
        a = self.make([updates(tg_update(100, tg_message(11))),
                       updates(tg_update(100, tg_message(11)))])
        _, minted = a.poll("50")
        self.assertEqual(self.http.requests[0][1].get("offset"), "50")
        self.assertEqual(minted, "101")
        # Re-poll with the SAME cursor — as the engine does after a crash before the
        # cursor commit. The offset must be 50 again, not 101: the platform re-serves
        # what was never acknowledged, and the store absorbs the duplicates (R9).
        again, minted2 = a.poll("50")
        self.assertEqual(self.http.requests[1][1].get("offset"), "50")
        self.assertEqual([m["ts"] for m in again], ["11"])
        self.assertEqual(minted2, "101")

    def test_the_first_poll_sends_no_offset(self):
        """No cursor yet means nothing has been acknowledged — sending an invented
        offset would destroy the pending queue unread."""
        a = self.make([updates()])
        a.poll(None)
        self.assertNotIn("offset", self.http.requests[0][1])

    def test_poll_makes_exactly_one_destructive_read_per_call(self):
        """In-poll pagination is UNSAFE here: a second getUpdates with an advanced
        offset acknowledges the first batch before the engine has committed it — a
        crash mid-drain loses that batch at the platform, unrecoverably. A backlog
        larger than one page drains across successive polls, each ratcheting the
        cursor only after the journal write."""
        limit = self.mod.PAGE_LIMIT
        full_page = updates(*[tg_update(100 + i, tg_message(11 + i))
                              for i in range(limit)])
        a = self.make([full_page])
        msgs, cursor = a.poll(None)
        self.assertEqual(len(msgs), limit)
        self.assertEqual(len(self.http.requests), 1,
                         "a full page tempted the adapter into a drain loop — the "
                         "second call's offset acknowledges uncommitted messages")
        self.assertEqual(cursor, str(100 + limit))

    def test_an_empty_poll_returns_the_cursor_unchanged(self):
        a = self.make([updates()])
        msgs, cursor = a.poll("50")
        self.assertEqual(msgs, [])
        self.assertEqual(cursor, "50")

    def test_a_junk_cursor_is_refused_not_guessed_at(self):
        """A cursor the adapter did not mint means state corruption somewhere;
        guessing an offset from it would acknowledge (destroy) unread messages."""
        a = self.make()
        with self.assertRaises(ValueError):
            a.poll("not-a-number")
        self.assertEqual(self.http.requests, [],
                         "the refusal must happen before any I/O — a getUpdates call "
                         "with a guessed offset is the destructive act itself")

    def test_updates_for_other_chats_advance_the_cursor_without_being_served(self):
        """The bot queue carries every chat the bot is in; the adapter watches one.
        Foreign rows are not ingested (the socket adapter's watched-channels rule),
        but the cursor must still advance past them or the poll loop wedges forever
        on chatter the instance will never store."""
        a = self.make([updates(tg_update(100, tg_message(11, chat="99999")),
                               tg_update(101, tg_message(12, text="mine")))])
        msgs, cursor = a.poll(None)
        self.assertEqual([m["text"] for m in msgs], ["mine"])
        self.assertEqual(cursor, "102")

    def test_the_ingest_set_is_pinned_at_the_platform(self):
        """allowed_updates makes the platform queue only what the adapter ingests —
        anything else would arrive, be dropped by us, and be destroyed on the next
        ack. Filtering at the source means nothing is silently discarded here."""
        a = self.make([updates()])
        a.poll(None)
        _, form = self.http.requests[0]
        self.assertEqual(json.loads(form["allowed_updates"]),
                         ["message", "edited_message"])

    def test_two_messages_in_the_same_second_stay_two_rows(self):
        """The store keys rows (channel_type, channel_id, ts) and message.date is
        whole seconds — ts must be the message_id (the platform's per-chat monotonic
        sequence) or same-second messages silently merge into one row."""
        a = self.make([updates(tg_update(100, tg_message(11, date=1700000000, text="a")),
                               tg_update(101, tg_message(12, date=1700000000, text="b")))])
        msgs, _ = a.poll(None)
        self.assertNotEqual(msgs[0]["ts"], msgs[1]["ts"])
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "m.db")
            try:
                store.upsert_messages(msgs)
                self.assertEqual(store.count("12345"), 2,
                                 "same-second messages merged — a date-based ts turns "
                                 "burst traffic into silent row loss")
            finally:
                store.close()

    def test_an_edit_lands_on_the_original_row_identity(self):
        """edited_message carries the SAME message_id, so an edit arrives with the
        original ts and new text — the store upserts in place and the journal records
        a revision (R23: an edit that turns a remark into an ask is a new ask)."""
        a = self.make([updates(tg_update(100, tg_message(11, text="deployed fine"))),
                       updates(tg_update(101, tg_message(11, text="please roll back"),
                                         kind="edited_message"))])
        first, cursor = a.poll(None)
        edited, _ = a.poll(cursor)
        self.assertEqual(first[0]["ts"], edited[0]["ts"])
        self.assertEqual(edited[0]["text"], "please roll back")

    def test_a_senderless_platform_row_still_meets_the_required_fields(self):
        """The store REQUIRES sender_id (R5). Channel-style posts carry sender_chat
        or nothing; map to something non-None, never drop the row."""
        anon = tg_message(11, text="from the group itself")
        del anon["from"]
        a = self.make([updates(tg_update(100, anon))])
        msgs, _ = a.poll(None)
        self.assertIsNotNone(msgs[0]["sender_id"])
        Store.validate(msgs[0])


class AttachmentTest(TelegramAdapterTestCase):
    """ENH-4: attachments are content. An image-only Telegram message has no text at
    all — dropping the media descriptor turns it into an empty row and the engine
    classifies it as an empty STATEMENT, acknowledged and forgotten."""

    def test_a_photo_is_normalized_into_an_image_attachment(self):
        photo = tg_message(11, text=None, caption="the dashboard",
                           photo=[{"file_id": "small", "width": 90, "height": 60},
                                  {"file_id": "big", "width": 900, "height": 600}])
        a = self.make([updates(tg_update(100, photo))])
        msgs, _ = a.poll(None)
        atts = msgs[0]["attachments"]
        self.assertEqual(len(atts), 1,
                         "photo[] is one image at several resolutions, not several "
                         "attachments")
        self.assertEqual(atts[0]["kind"], "image")
        self.assertEqual(msgs[0]["text"], "the dashboard",
                         "a media caption is the message's text")
        Store.validate(msgs[0])

    def test_a_document_keeps_its_name_and_mimetype_as_a_file(self):
        doc = tg_message(11, text=None,
                         document={"file_id": "f1", "file_name": "report.pdf",
                                   "mime_type": "application/pdf"})
        a = self.make([updates(tg_update(100, doc))])
        msgs, _ = a.poll(None)
        self.assertEqual(msgs[0]["attachments"][0],
                         {"kind": "file", "name": "report.pdf",
                          "mimetype": "application/pdf", "url": None})

    def test_attachment_urls_are_never_minted(self):
        """Telegram file URLs embed the bot token (api.telegram.org/file/bot<TOKEN>/…)
        — a url field here would persist a live credential into every adopter's store.
        The file_id in raw is the retrieval handle; url stays None."""
        voice = tg_message(11, text=None,
                           voice={"file_id": "v1", "mime_type": "audio/ogg"})
        a = self.make([updates(tg_update(100, voice))])
        msgs, _ = a.poll(None)
        for att in msgs[0]["attachments"]:
            self.assertIsNone(att["url"])
        self.assertNotIn("tok", json.dumps(msgs[0]["attachments"]))

    def test_a_plain_text_message_carries_a_known_empty_list(self):
        a = self.make([updates(tg_update(100, tg_message(11)))])
        msgs, _ = a.poll(None)
        self.assertEqual(msgs[0]["attachments"], [])


class RateLimitTest(TelegramAdapterTestCase):
    def test_429_surfaces_the_bodys_retry_after_and_names_the_method(self):
        """The wait lives in the JSON body (parameters.retry_after) — Telegram's
        dialect, not Slack's header. The header is planted with a DIFFERENT value so
        a helper that silently reads headers cannot pass this test."""
        body = {"ok": False, "error_code": 429,
                "description": "Too Many Requests: retry after 41",
                "parameters": {"retry_after": 41}}
        a = self.make([(429, {"Retry-After": "99"}, body)])
        with self.assertRaises(RateLimited) as ctx:
            a.poll(None)
        self.assertEqual(ctx.exception.retry_after, 41.0)
        self.assertEqual(ctx.exception.method, "getUpdates")

    def test_a_429_stripped_of_its_body_falls_back_to_the_header(self):
        a = self.make([(429, {"Retry-After": "7"}, "")])
        with self.assertRaises(RateLimited) as ctx:
            a.poll(None)
        self.assertEqual(ctx.exception.retry_after, 7.0)

    def test_health_detail_exposes_an_active_hold(self):
        """Contract rule 3: the adapter never sleeps, but it must SHOW the back-off so
        an operator reading health output knows why polling has gone quiet."""
        now = [100.0]
        body = {"ok": False, "error_code": 429, "description": "Too Many Requests",
                "parameters": {"retry_after": 30}}
        a = self.make([(429, {}, body),
                       (200, {}, {"ok": True, "result": {"username": "watcher_bot"}})],
                      clock=lambda: now[0])
        with self.assertRaises(RateLimited):
            a.poll(None)
        self.assertIn("getUpdates", a.health()["detail"])
        now[0] = 200.0   # hold expired — the detail must not report it forever
        self.http.script = [(200, {}, {"ok": True,
                                       "result": {"username": "watcher_bot"}})]
        self.assertNotIn("getUpdates", a.health()["detail"])


class HealthTest(TelegramAdapterTestCase):
    def test_health_passes_on_get_me_ok(self):
        a = self.make([(200, {}, {"ok": True, "result": {"id": 1, "is_bot": True,
                                                         "username": "watcher_bot"}})])
        h = a.health()
        self.assertTrue(h["reachable"])
        self.assertTrue(h["auth_ok"])

    def test_health_fails_on_a_bad_token(self):
        """Contract rule 5: a health check that can only pass is a defect."""
        a = self.make([(401, {}, {"ok": False, "error_code": 401,
                                  "description": "Unauthorized"})])
        h = a.health()
        self.assertTrue(h["reachable"])
        self.assertFalse(h["auth_ok"])
        self.assertIn("Unauthorized", h["detail"])

    def test_health_fails_when_the_platform_is_unreachable(self):
        a = self.make([OSError("no route to host")])
        h = a.health()
        self.assertFalse(h["reachable"])
        self.assertFalse(h["auth_ok"])

    def test_health_never_touches_the_destructive_queue(self):
        """getUpdates is single-consumer (a concurrent caller 409s the live poll) and
        is the one surface whose reads acknowledge. Liveness must cost neither."""
        a = self.make([(200, {}, {"ok": True, "result": {"username": "watcher_bot"}})])
        a.health()
        url, _ = self.http.requests[0]
        self.assertTrue(url.endswith("/getMe"))


class ResolveTest(TelegramAdapterTestCase):
    def test_a_chat_id_resolves_to_its_title(self):
        a = self.make([(200, {}, {"ok": True, "result": {"id": -100987,
                                                         "title": "ops-room",
                                                         "type": "supergroup"}})])
        self.assertEqual(a.resolve("-100987"), "ops-room")

    def test_a_username_resolves_to_its_numeric_id(self):
        a = self.make([(200, {}, {"ok": True, "result": {"id": -100987,
                                                         "title": "ops-room",
                                                         "type": "supergroup"}})])
        self.assertEqual(a.resolve("@opsroom"), "-100987")

    def test_an_unresolvable_ref_raises_naming_it(self):
        a = self.make([(400, {}, {"ok": False, "error_code": 400,
                                  "description": "Bad Request: chat not found"})])
        with self.assertRaises(LookupError) as ctx:
            a.resolve("@nobody-here")
        self.assertIn("nobody-here", str(ctx.exception))


class NoSnapshotCapabilityTest(TelegramAdapterTestCase):
    """The design finding that reshaped the roadmap (state/DESIGN-ENH-25-telegram.md):
    retrievable_ts is IMPOSSIBLE on this platform — a bot cannot ask for anything it
    has acknowledged. Parity against a Telegram store is therefore permanently
    fail-closed, and ENH-27's declaration is what keeps an operator from reading the
    resulting ENGINE_LOST rows as a read-path defect (the misreading that cost a day
    on R8)."""

    def test_retrievable_ts_stays_absent(self):
        """Absent, not stubbed: a stub returning an empty set would mark every
        still-real message 'deleted upstream' — the one direction that hides a loss."""
        self.assertFalse(hasattr(self.make(), "retrievable_ts"))

    def test_the_capability_gap_is_declared_by_name(self):
        declaration = snapshot_declaration(self.make(), "telegram")
        self.assertIsNotNone(declaration,
                             "core/parity no longer declares this adapter's missing "
                             "snapshot — the first live parity run will be misread as "
                             "a read-path defect")
        self.assertIn("telegram", declaration)
        self.assertIn("retrievable_ts", declaration)


if __name__ == "__main__":
    unittest.main(verbosity=2)
