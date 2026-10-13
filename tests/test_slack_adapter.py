"""Slack adapter tests (ENH-18 read path, feeding gates G1/R8/R17; ENH-31 send path).

The adapter under test is the first REAL platform adapter. It landed with no send path at
any layer, because this host's token was authorized READ-ONLY (2026-08-26); ENH-31 added
exactly one write, `chat.postMessage`, for the aws-do-agent harness's bot token, and the
tested invariants became:

* `send()` and `read_back()` exist but are callable only by core/outbox at policy
  `staged` (a human click); send capability is declared for a bot token (`xoxb-`) only;
* the transport itself default-denies — any Web API method outside the read allowlist
  plus that single write is refused BEFORE a byte reaches the wire, so a future helper
  cannot smuggle another write through the generic `_api` primitive;
* polling is gap-free (pagination followed to the end, or fail loudly — never a silent
  truncation) and cursored per channel;
* a platform 429 surfaces as core.ratelimit.RateLimited carrying the platform's EXACT
  Retry-After and the METHOD it hit, because the engine's back-off is keyed
  (instance, method) (ENH-1) and an unlabelled 429 would collapse that scope;
* health can FAIL (contract rule 5 — this repo exists partly because of a check that
  could only pass, docs/PROVENANCE.md).

All network I/O is faked via the injectable transport; nothing here touches Slack.
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
from core.ratelimit import RateLimited  # noqa: E402
from core.store import Store  # noqa: E402

ADAPTER_PY = ROOT / "channels" / "slack" / "adapter.py"


def _load_module():
    """The module itself (not just the Adapter class) — the tests pin the transport
    guard exceptions, which load_adapter_class deliberately does not expose."""
    spec = importlib.util.spec_from_file_location("slack_adapter_under_test", ADAPTER_PY)
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


def slack_msg(ts, user="U_A", text="hello", **extra):
    """A raw Slack history entry, as the platform sends it."""
    m = {"type": "message", "ts": ts, "user": user, "text": text}
    m.update(extra)
    return m


def history(messages, has_more=False, next_cursor=None):
    body = {"ok": True, "messages": messages, "has_more": has_more}
    if next_cursor is not None:
        body["response_metadata"] = {"next_cursor": next_cursor}
    return (200, {}, body)


class SlackAdapterTestCase(unittest.TestCase):
    def setUp(self):
        self.mod = _load_module()

    def make(self, script=(), channels="C_ONE", clock=None):
        self.http = FakeHttp(script)
        kwargs = {"clock": clock} if clock else {}
        return self.mod.Adapter(auth={"token": "xoxb-tok", "channels": channels},
                                http=self.http, **kwargs)


class ContractSurfaceTest(SlackAdapterTestCase):
    def test_slack_is_discovered_from_the_shipped_tree(self):
        """R11 discovery is the landing mechanism — a dir-drop, zero core/ changes."""
        self.assertIn("slack", discover_adapters(ROOT / "channels"))
        cls = load_adapter_class(ROOT / "channels", "slack")
        self.assertTrue(callable(cls))

    def test_capabilities_answer_every_contract_key_send_on_direct_off(self):
        """ENH-31: send exists — behind core/outbox, at policy `staged` only. `direct`
        is declared OFF so an outbox built for a direct policy refuses at construction:
        the 2026-10-13 grant is a human click, never the engine posting on its own."""
        caps = self.make().capabilities()
        for k in ("read", "history", "search", "send", "react", "threads", "direct"):
            self.assertIn(k, caps)
        self.assertTrue(caps["read"])
        self.assertTrue(caps["history"])
        self.assertTrue(caps["send"])
        self.assertTrue(caps["threads"], "replies are placed with thread_ts")
        self.assertFalse(caps["direct"], "the engine must never post without a human")
        self.assertFalse(caps["react"])

    def test_send_and_read_back_are_the_only_write_surface(self):
        """One authorized write (chat.postMessage, through send()) and its proof
        (read_back). Nothing else public looks like a write: no post/update/delete/
        react/publish surface for any caller — outbox included — to find."""
        a = self.make()
        self.assertTrue(callable(a.send))
        self.assertTrue(callable(a.read_back))
        for name in dir(a):
            if name.startswith("_") or name in ("send", "read_back"):
                continue
            for banned in ("send", "post", "write", "publish", "react", "update",
                           "delete", "upload"):
                self.assertNotIn(banned, name.lower(),
                                 f"public attribute {name!r} looks like a write surface")
        self.assertEqual(self.mod.WRITE_METHODS, frozenset({"chat.postMessage"}))

    def test_missing_token_or_channels_refuses_at_construction(self):
        """Half-configured is the worst state to discover during an incident — the
        same load-time-refusal rule core/config.py applies to env vars."""
        with self.assertRaises(ValueError) as ctx:
            self.mod.Adapter(auth={"channels": "C_ONE"})
        self.assertIn("token", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            self.mod.Adapter(auth={"token": "tok"})
        self.assertIn("channels", str(ctx.exception))


class ApiBaseTest(SlackAdapterTestCase):
    """COMMS_SLACK_API_BASE points the adapter at a LOOPBACK stub for hermetic tests
    (ENH-31 C). The default is unchanged; anything but http://127.0.0.1:<port> or
    http://localhost:<port> is refused at construction, by name, before any I/O — the
    override must never become a way to send a workspace token to a third host."""

    def make_env(self, env, script=()):
        self.http = FakeHttp(script)
        return self.mod.Adapter(auth={"token": "xoxb-tok", "channels": "C_ONE"},
                                http=self.http, env=env)

    def test_the_default_base_is_slack(self):
        a = self.make_env({}, [(200, {}, {"ok": True})])
        a._api("auth.test")
        self.assertEqual(self.http.requests[0][0], "https://slack.com/api/auth.test")

    def test_a_loopback_base_is_used_for_every_call(self):
        a = self.make_env({"COMMS_SLACK_API_BASE": "http://127.0.0.1:18999"},
                          [(200, {}, {"ok": True})])
        a._api("auth.test")
        self.assertEqual(self.http.requests[0][0], "http://127.0.0.1:18999/auth.test")
        a = self.make_env({"COMMS_SLACK_API_BASE": "http://127.0.0.1:18999/"},
                          [(200, {}, {"ok": True})])
        a._api("auth.test")
        self.assertEqual(self.http.requests[0][0], "http://127.0.0.1:18999/auth.test")

    def test_health_says_when_an_api_base_override_is_active(self):
        """Review N5: a leftover test variable on a live host must be visible — health()
        is what the operator reads."""
        a = self.make_env({"COMMS_SLACK_API_BASE": "http://127.0.0.1:18999"},
                          [(200, {}, {"ok": True, "user": "comms-bot"})])
        detail = a.health()["detail"]
        self.assertIn("override active", detail)
        self.assertIn("127.0.0.1:18999", detail)
        a = self.make_env({}, [(200, {}, {"ok": True, "user": "comms-bot"})])
        self.assertNotIn("override", a.health()["detail"])

    def test_a_non_loopback_base_is_refused_by_name_before_any_io(self):
        for bad in ("http://10.0.0.5:8080", "https://127.0.0.1:18999", "http://evil.example",
                    "http://127.0.0.1", "http://127.0.0.1:1/api", "http://u@127.0.0.1:1",
                    "http://127.0.0.1:1/?x=1", "ftp://127.0.0.1:1",
                    "http://localhost:18999"):   # a NAME resolves wherever /etc/hosts says (N5)
            with self.assertRaises(ValueError, msg=bad) as ctx:
                self.make_env({"COMMS_SLACK_API_BASE": bad})
            self.assertIn("COMMS_SLACK_API_BASE", str(ctx.exception))
            self.assertEqual(self.http.requests, [], "a refused base still caused I/O")


class ReadOnlyTransportTest(SlackAdapterTestCase):
    """Deny-by-default at the layer every request must pass through. Without this, the
    no-send property above is one convenience wrapper away from being lost."""

    REFUSED_METHODS = ("chat.update", "chat.delete", "chat.meMessage", "chat.scheduleMessage",
                       "reactions.add", "files.upload", "conversations.join", "pins.add")

    def test_every_write_but_chat_postMessage_is_refused_before_any_io(self):
        a = self.make()
        for method in self.REFUSED_METHODS:
            with self.assertRaises(self.mod.ReadOnlyViolation, msg=method):
                a._api(method, channel="C_ONE", text="never")
        self.assertEqual(self.http.requests, [],
                         "a refused method still reached the wire — the guard runs "
                         "after I/O, which is no guard at all")

    def test_read_methods_pass_the_guard(self):
        a = self.make([(200, {}, {"ok": True})])
        a._api("auth.test")
        self.assertEqual(len(self.http.requests), 1)


def posted(ts="1700000100.000100", channel="C_ONE"):
    return (200, {}, {"ok": True, "channel": channel, "ts": ts,
                      "message": {"type": "message", "ts": ts, "text": "x"}})


class SendTest(SlackAdapterTestCase):
    """The send path (ENH-31 A), adapter side: one chat.postMessage carrying the outbox
    key in message METADATA, the token in the header, placement via thread_ts; read_back
    proves the SAME message (key and canonical text) came back; newest() is the pre-send
    re-read. No guard lives here — the outbox refuses before the INTENT flip."""

    def test_send_posts_once_with_the_key_in_metadata_and_the_token_in_the_header(self):
        a = self.make([posted()])
        receipt = a.send("C_ONE", "[AGENT] here: http://127.0.0.1:8502", key="k1",
                         thread_id="1700000000.000100")
        self.assertEqual(receipt["ts"], "1700000100.000100")
        self.assertEqual(len(self.http.requests), 1)
        url, form = self.http.requests[0]
        self.assertTrue(url.endswith("/chat.postMessage"))
        self.assertEqual(form["channel"], "C_ONE")
        self.assertEqual(form["text"], "[AGENT] here: http://127.0.0.1:8502")
        self.assertEqual(form["thread_ts"], "1700000000.000100")
        self.assertEqual(json.loads(form["metadata"])["event_payload"], {"key": "k1"})
        self.assertNotIn("token", form, "the token travels in the Authorization header only")

    def test_a_top_level_send_carries_no_thread_ts(self):
        a = self.make([posted()])
        a.send("C_ONE", "[AGENT] top-level", key="k2")
        self.assertNotIn("thread_ts", self.http.requests[0][1])

    def test_read_back_proves_key_and_canonical_text_of_what_this_process_sent(self):
        ts = "1700000100.000100"
        meta = {"event_type": "comms_engine_reply", "event_payload": {"key": "k1"}}
        shown = {"type": "message", "ts": ts, "user": "U_BOT", "metadata": meta,
                 "text": "[AGENT] see &lt;http://127.0.0.1:8502&gt; &amp; reply"}
        a = self.make([posted(ts), history([shown])])
        a.send("C_ONE", "[AGENT] see <http://127.0.0.1:8502> & reply", key="k1")
        self.assertTrue(a.read_back("C_ONE", "k1"))
        form = self.http.requests[1][1]
        self.assertEqual((form["latest"], form["oldest"], form["inclusive"],
                          form["include_all_metadata"]), (ts, ts, "true", "true"))

    def test_read_back_raises_when_the_platform_shows_different_text(self):
        """Not False (that would read as 'never sent' and recovery would post again,
        review F3) but the third answer: present, with other text."""
        from core.delivery import DeliveryMismatch
        ts = "1700000100.000100"
        meta = {"event_type": "comms_engine_reply", "event_payload": {"key": "k1"}}
        shown = {"type": "message", "ts": ts, "user": "U_BOT", "metadata": meta,
                 "text": "[AGENT] a TRUNCATED or rewritten"}
        a = self.make([posted(ts), history([shown])])
        a.send("C_ONE", "[AGENT] the approved text, in full", key="k1")
        with self.assertRaises(DeliveryMismatch):
            a.read_back("C_ONE", "k1")

    def test_read_back_fails_when_the_shown_message_carries_another_key(self):
        ts = "1700000100.000100"
        meta = {"event_type": "comms_engine_reply", "event_payload": {"key": "OTHER"}}
        shown = {"type": "message", "ts": ts, "user": "U_BOT", "metadata": meta,
                 "text": "[AGENT] same text"}
        a = self.make([posted(ts), history([shown])])
        a.send("C_ONE", "[AGENT] same text", key="k1")
        self.assertFalse(a.read_back("C_ONE", "k1"))

    def test_read_back_after_a_restart_searches_the_channel_or_the_thread_by_key(self):
        """Recovery: a new process holds no ts for the key. The newest page of history
        (top-level) or the thread (when the caller passes thread_id) is searched."""
        meta = {"event_type": "comms_engine_reply", "event_payload": {"key": "k9"}}
        a = self.make([history([{"type": "message", "ts": "5.0", "user": "U_BOT",
                                 "metadata": meta, "text": "[AGENT] x"}])])
        self.assertTrue(a.read_back("C_ONE", "k9"))
        self.assertEqual(self.http.requests[0][1]["include_all_metadata"], "true")
        a = self.make([history([])])
        self.assertFalse(a.read_back("C_ONE", "k9"))
        thread = {"ok": True, "has_more": False, "messages": [
            {"type": "message", "ts": "1.0", "user": "U_A", "text": "ask?"},
            {"type": "message", "ts": "2.0", "user": "U_BOT", "thread_ts": "1.0",
             "metadata": meta, "text": "[AGENT] x"}]}
        a = self.make([(200, {}, thread)])
        self.assertTrue(a.read_back("C_ONE", "k9", thread_id="1.0"))
        self.assertTrue(self.http.requests[0][0].endswith("/conversations.replies"))

    def test_only_a_definite_refusal_of_the_post_is_not_posted(self):
        """Review round 3 (R3-3): NotPosted is an allowlist. internal_error, fatal_error and
        a non-JSON 5xx may have posted; they stay ApiError so the row stays in flight."""
        from core.delivery import NotPosted
        a = self.make([(200, {}, {"ok": False, "error": "not_in_channel"})])
        with self.assertRaises(NotPosted):
            a.send("C_ONE", "[AGENT] x", key="k1")
        for answer in ((200, {}, {"ok": False, "error": "internal_error"}),
                       (200, {}, {"ok": False, "error": "fatal_error"}),
                       (504, {}, "<html>gateway timeout</html>")):
            with self.subTest(answer=answer):
                a = self.make([answer])
                with self.assertRaises(self.mod.ApiError) as cm:
                    a.send("C_ONE", "[AGENT] x", key="k1")
                self.assertNotIsInstance(cm.exception, NotPosted)

    def test_a_gone_target_on_read_back_is_target_gone_and_an_outage_is_not(self):
        """Review round 3 (R3-5): thread_not_found / is_archived mean the reply can never be
        placed — recovery parks on them; anything else is transient, channel_not_found
        included, since Slack also answers it for a private channel the token temporarily
        cannot see (round 4, R4-8)."""
        from core.delivery import TargetGone
        for error in ("thread_not_found", "is_archived"):
            with self.subTest(error=error):
                a = self.make([(200, {}, {"ok": False, "error": error})])
                with self.assertRaises(TargetGone):
                    a.read_back("C_ONE", "k9", thread_id="1.0")
        for error in ("service_unavailable", "channel_not_found"):
            with self.subTest(error=error):
                a = self.make([(200, {}, {"ok": False, "error": error})])
                with self.assertRaises(self.mod.ApiError) as cm:
                    a.read_back("C_ONE", "k9", thread_id="1.0")
                self.assertNotIsInstance(cm.exception, TargetGone)

    def test_a_thread_read_back_asks_for_message_metadata(self):
        """Review F2: conversations.replies serves metadata only when asked, exactly like
        history. Without the flag every thread read-back is 'unproven' and recovery
        posts the reply again."""
        ts = "1700000100.000100"
        meta = {"event_type": "comms_engine_reply", "event_payload": {"key": "k1"}}
        thread = {"ok": True, "has_more": False, "messages": [
            slack_msg("1.0", text="ask?"),
            {"type": "message", "ts": ts, "user": "U_BOT", "thread_ts": "1.0",
             "metadata": meta, "text": "[AGENT] x"}]}
        a = self.make([posted(ts), (200, {}, thread)])
        a.send("C_ONE", "[AGENT] x", key="k1", thread_id="1.0")
        self.assertTrue(a.read_back("C_ONE", "k1"))
        form = self.http.requests[1][1]
        self.assertTrue(self.http.requests[1][0].endswith("/conversations.replies"))
        self.assertEqual(form["include_all_metadata"], "true")
        a = self.make([(200, {}, thread)])
        self.assertTrue(a.read_back("C_ONE", "k1", thread_id="1.0"))
        self.assertEqual(self.http.requests[0][1]["include_all_metadata"], "true")

    def test_a_message_with_the_key_but_other_text_is_a_mismatch_not_absence(self):
        """Review F3: False means 'no message carries the key' and makes recovery
        re-send. A present message with different text is a third answer — raised, so
        the outbox parks the row instead of posting again or committing the wrong text."""
        from core.delivery import DeliveryMismatch
        ts = "1700000100.000100"
        meta = {"event_type": "comms_engine_reply", "event_payload": {"key": "k1"}}
        shown = {"type": "message", "ts": ts, "user": "U_BOT", "metadata": meta,
                 "text": "[AGENT] a TRUNCATED"}
        a = self.make([posted(ts), history([shown])])
        a.send("C_ONE", "[AGENT] the approved text, in full", key="k1")
        with self.assertRaises(DeliveryMismatch):
            a.read_back("C_ONE", "k1")
        # after a restart, the outbox passes the approved text: same verdict
        a = self.make([history([shown])])
        with self.assertRaises(DeliveryMismatch):
            a.read_back("C_ONE", "k1", text="[AGENT] the approved text, in full")
        # with no text to compare, the key alone proves presence (the old contract)
        a = self.make([history([shown])])
        self.assertTrue(a.read_back("C_ONE", "k1"))

    def test_canonical_compares_content_not_slacks_link_storage(self):
        """Review F8: Slack auto-links a bare domain or an email as <target|label> with
        the typed text as the LABEL, and wraps a typed URL as <url>. The canonical form is
        the content the human typed, or a reply naming a bare domain fails its own
        read-back."""
        c = self.mod._canonical
        self.assertEqual(c("[AGENT] here: <http://127.0.0.1:8502>"), c("[AGENT] here: http://127.0.0.1:8502"))
        self.assertEqual(c("[AGENT] see <http://docs.example|docs.example>"), c("[AGENT] see docs.example"))
        self.assertEqual(c("[AGENT] mail <mailto:ops@example.com|ops@example.com>"), c("[AGENT] mail ops@example.com"))
        self.assertEqual(c("[AGENT] <http://127.0.0.1:8502/?a=1&amp;b=2>"), c("[AGENT] http://127.0.0.1:8502/?a=1&b=2"))
        self.assertEqual(c("[AGENT] mail <mailto:ops@example.com>"), c("[AGENT] mail ops@example.com"),
                         "an unlabelled mailto link (typed as such) keeps its scheme — review R2-7 Y2")
        self.assertNotEqual(c("[AGENT] see docs.example"), c("[AGENT] see docs.other"))

    def test_newest_reads_the_channel_top_level_or_one_thread(self):
        a = self.make([history([slack_msg("9.0", user="U_H", text="newer")])])
        m = a.newest("C_ONE")
        self.assertEqual((m["ts"], m["sender_id"]), ("9.0", "U_H"))
        self.assertEqual(self.http.requests[0][1]["include_all_metadata"], "true")
        thread = {"ok": True, "has_more": False, "messages": [
            slack_msg("1.0", text="ask?"),
            slack_msg("2.0", user="U_B", text="r1", thread_ts="1.0"),
            slack_msg("3.0", user="U_C", text="r2", thread_ts="1.0")]}
        a = self.make([(200, {}, thread)])
        self.assertEqual(a.newest("C_ONE", thread_id="1.0")["ts"], "3.0")
        a = self.make([history([])])
        self.assertIsNone(a.newest("C_ONE"))

    def test_newest_skips_this_engines_own_posts(self):
        """Review F4: the engine's own earlier reply is a message too; without this the
        first approved Send refused the second ("re-read first: U_BOT posted...")."""
        meta = {"event_type": "comms_engine_reply", "event_payload": {"key": "k1"}}
        a = self.make([history([{"type": "message", "ts": "9.0", "user": "U_BOT",
                                 "metadata": meta, "text": "[AGENT] our reply"},
                                slack_msg("8.0", user="U_H", text="the human's last word")])])
        self.assertEqual(a.newest("C_ONE")["ts"], "8.0")
        self.assertGreaterEqual(int(self.http.requests[0][1]["limit"]), 2,
                                "one message cannot show what hides behind our own post "
                                "(review R2-7 Y3)")
        a = self.make([history([{"type": "message", "ts": "9.0", "user": "U_BOT",
                                 "metadata": meta, "text": "[AGENT] our reply"}])])
        self.assertIsNone(a.newest("C_ONE"))

    def test_newest_in_a_thread_asks_for_metadata_and_skips_this_engines_own_reply(self):
        """Review R2-7 (Y5): the thread re-read must ask for metadata, or the engine's own
        earlier thread reply refuses the next one ("re-read first: U_BOT posted...")."""
        meta = {"event_type": "comms_engine_reply", "event_payload": {"key": "k1"}}
        thread = {"ok": True, "has_more": False, "messages": [
            slack_msg("1.0", text="ask?"),
            slack_msg("2.0", user="U_H", text="the human's last word", thread_ts="1.0"),
            {"type": "message", "ts": "3.0", "user": "U_BOT", "thread_ts": "1.0",
             "metadata": meta, "text": "[AGENT] our earlier reply"}]}
        a = self.make([(200, {}, thread)])
        self.assertEqual(a.newest("C_ONE", thread_id="1.0")["ts"], "2.0")
        self.assertEqual(self.http.requests[0][1]["include_all_metadata"], "true")

    def test_send_is_declared_only_for_a_bot_token(self):
        """Review N1: the grant is the harness's BOT identity. A user token would post in
        a person's name — its drafts stay Discard-only because send is not declared."""
        self.http = FakeHttp()
        user = self.mod.Adapter(auth={"token": "xoxp-user", "channels": "C_ONE"}, http=self.http)
        self.assertFalse(user.capabilities()["send"])
        self.assertTrue(self.make().capabilities()["send"])

    def test_a_restart_read_back_searches_from_the_drafts_age_all_pages(self):
        """Review N8: the newest 200 messages missed an older post and re-sent it; with
        `since` from the outbox row the search starts at the draft's age minus a margin
        and follows every page."""
        meta = {"event_type": "comms_engine_reply", "event_payload": {"key": "k9"}}
        page1 = {"ok": True, "has_more": True, "response_metadata": {"next_cursor": "c2"},
                 "messages": [slack_msg("5000.0", text="newer noise")]}
        page2 = {"ok": True, "has_more": False, "messages": [
            {"type": "message", "ts": "4000.0", "user": "U_BOT", "metadata": meta,
             "text": "[AGENT] x"}]}
        a = self.make([(200, {}, page1), (200, {}, page2)])
        self.assertTrue(a.read_back("C_ONE", "k9", since=3900.0))
        first = self.http.requests[0][1]
        self.assertEqual((first["oldest"], first["include_all_metadata"]), ("3600.000000", "true"))
        self.assertEqual(self.http.requests[1][1]["cursor"], "c2")

    def test_the_size_limit_is_declared_for_the_outbox(self):
        self.assertEqual(self.mod.MAX_TEXT_CHARS, 3500)
        self.assertEqual(self.mod.Adapter.MAX_TEXT_CHARS if hasattr(self.mod.Adapter, "MAX_TEXT_CHARS") else self.mod.MAX_TEXT_CHARS, 3500)


class PollTest(SlackAdapterTestCase):
    def test_poll_normalizes_to_the_pinned_contract_and_the_store_accepts_it(self):
        parent = slack_msg("3.0", thread_ts="3.0")   # a thread PARENT: thread_ts == ts
        reply_broadcast = slack_msg("4.0", thread_ts="3.0")
        a = self.make([history([reply_broadcast, parent, slack_msg("2.0")])])
        msgs, cursor = a.poll(None)

        self.assertEqual([m["ts"] for m in msgs], ["2.0", "3.0", "4.0"],
                         "history pages arrive newest-first; the engine contract is "
                         "sortable oldest-first")
        for m in msgs:
            self.assertEqual(m["channel_type"], "slack")
            self.assertEqual(m["channel_id"], "C_ONE")
            self.assertIsInstance(m["raw"], str,
                                  "raw must be a JSON string — the store binds it "
                                  "straight into sqlite")
        # Slack marks a thread parent with thread_ts == its own ts; the contract says
        # thread_id is null for top-level messages.
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

    def test_poll_is_gap_free_across_pages(self):
        """The live poller depends on this (R9): stopping at page one silently loses
        every older message in the window."""
        a = self.make([
            history([slack_msg("9.0"), slack_msg("8.0")], has_more=True, next_cursor="p2"),
            history([slack_msg("7.0")]),
        ])
        msgs, cursor = a.poll(None)
        self.assertEqual([m["ts"] for m in msgs], ["7.0", "8.0", "9.0"])
        self.assertEqual(json.loads(cursor), {"C_ONE": "9.0"})
        self.assertEqual(self.http.requests[1][1].get("cursor"), "p2",
                         "the follow-up request did not carry the page cursor")

    def test_a_broken_pagination_chain_fails_loudly_never_truncates(self):
        a = self.make([history([slack_msg("9.0")], has_more=True)])  # no next_cursor
        with self.assertRaises(self.mod.ApiError):
            a.poll(None)

    def test_cursor_is_per_channel_and_polls_resume_exclusively(self):
        page = {"C_ONE": history([slack_msg("5.0")]),
                "C_TWO": history([slack_msg("3.0")])}
        a = self.make(channels="C_ONE,C_TWO")
        self.http.script = [page["C_ONE"], page["C_TWO"]]
        _, cursor = a.poll(None)
        self.assertEqual(json.loads(cursor), {"C_ONE": "5.0", "C_TWO": "3.0"})

        # Resume: each channel must be asked from ITS OWN offset — a shared/global
        # offset would skip C_TWO's 3.0→5.0 window entirely.
        b = self.make([history([]), history([slack_msg("4.0")])],
                      channels="C_ONE,C_TWO")
        msgs, cursor2 = b.poll(cursor)
        oldest_by_channel = {form["channel"]: form.get("oldest")
                             for _, form in self.http.requests}
        self.assertEqual(oldest_by_channel, {"C_ONE": "5.0", "C_TWO": "3.0"})
        self.assertEqual([m["ts"] for m in msgs], ["4.0"])
        self.assertEqual(json.loads(cursor2), {"C_ONE": "5.0", "C_TWO": "4.0"})

    def test_an_empty_poll_returns_the_cursor_unchanged(self):
        a = self.make([history([])])
        msgs, cursor = a.poll('{"C_ONE": "5.0"}')
        self.assertEqual(msgs, [])
        self.assertEqual(cursor, '{"C_ONE": "5.0"}')

    def test_a_junk_cursor_is_refused_not_guessed_at(self):
        """A cursor the adapter did not mint means state corruption somewhere; guessing
        a window from it could silently re-read or skip history."""
        a = self.make()
        with self.assertRaises(ValueError):
            a.poll("not-json")

    def test_a_senderless_platform_row_still_meets_the_required_fields(self):
        """The store REQUIRES sender_id (R5). Slack bot/system rows carry bot_id or
        nothing; the adapter must map them to something non-None, never drop the row."""
        bot_row = {"type": "message", "ts": "6.0", "text": "from a bot",
                   "bot_id": "B_X"}
        a = self.make([history([bot_row])])
        msgs, _ = a.poll(None)
        self.assertEqual(msgs[0]["sender_id"], "B_X")
        Store.validate(msgs[0])


class AttachmentTest(SlackAdapterTestCase):
    """ENH-4: uploads ride the platform's `files` array, and the live system downloads
    those screenshots and treats them as content. An adapter that normalizes only text
    turns an image-only message into an empty row — represented, but saying nothing."""

    FILE = {"id": "F_X", "name": "screenshot.png", "title": "screenshot",
            "mimetype": "image/png", "url_private": "https://files.example/x.png"}

    def test_an_upload_is_normalized_into_attachments(self):
        row = slack_msg("7.0", text="", files=[self.FILE])
        a = self.make([history([row])])
        msgs, _ = a.poll(None)
        atts = msgs[0]["attachments"]
        self.assertEqual(len(atts), 1)
        self.assertEqual(atts[0]["kind"], "image")
        self.assertEqual(atts[0]["name"], "screenshot.png")
        self.assertEqual(atts[0]["mimetype"], "image/png")
        self.assertEqual(atts[0]["url"], "https://files.example/x.png")
        Store.validate(msgs[0])

    def test_a_non_image_upload_is_kept_as_a_file(self):
        pdf = dict(self.FILE, mimetype="application/pdf", name="report.pdf")
        a = self.make([history([slack_msg("7.1", files=[pdf])])])
        msgs, _ = a.poll(None)
        self.assertEqual(msgs[0]["attachments"][0]["kind"], "file",
                         "kind must degrade honestly for anything that is not an "
                         "image, never invent a category from the mimetype prefix")

    def test_a_fileless_message_carries_a_known_empty_list(self):
        """[] is the adapter saying 'I looked and there were none' — distinct from a
        row that predates the field (store keeps None for those)."""
        a = self.make([history([slack_msg("7.2")])])
        msgs, _ = a.poll(None)
        self.assertEqual(msgs[0]["attachments"], [])


class RateLimitTest(SlackAdapterTestCase):
    def test_429_surfaces_the_exact_retry_after_and_names_the_method(self):
        """ENH-1's back-off is keyed (instance, method); a 429 that loses either the
        platform's number or the method identity collapses that scope."""
        a = self.make([(429, {"Retry-After": "37"}, {"ok": False, "error": "ratelimited"})])
        with self.assertRaises(RateLimited) as ctx:
            a.poll(None)
        self.assertEqual(ctx.exception.retry_after, 37.0)
        self.assertEqual(ctx.exception.method, "conversations.history")

    def test_a_200_ratelimited_body_is_also_surfaced_as_ratelimited(self):
        a = self.make([(200, {"Retry-After": "2"}, {"ok": False, "error": "ratelimited"})])
        with self.assertRaises(RateLimited) as ctx:
            a.poll(None)
        self.assertEqual(ctx.exception.retry_after, 2.0)

    def test_health_detail_exposes_an_active_hold(self):
        """Contract rule 3: the adapter never sleeps, but it must SHOW the back-off so
        an operator reading health output knows why polling has gone quiet."""
        now = [100.0]
        a = self.make([(429, {"Retry-After": "30"}, ""),
                       (200, {}, {"ok": True, "user": "watcher"})],
                      clock=lambda: now[0])
        with self.assertRaises(RateLimited):
            a.poll(None)
        h = a.health()
        self.assertIn("conversations.history", h["detail"])
        now[0] = 200.0   # hold expired — the detail must not report it forever
        a.http = self.http
        self.http.script = [(200, {}, {"ok": True, "user": "watcher"})]
        self.assertNotIn("conversations.history", a.health()["detail"])


class HealthTest(SlackAdapterTestCase):
    def test_health_passes_on_auth_test_ok(self):
        a = self.make([(200, {}, {"ok": True, "user": "watcher", "team": "T_X"})])
        h = a.health()
        self.assertTrue(h["reachable"])
        self.assertTrue(h["auth_ok"])

    def test_health_fails_on_bad_auth(self):
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


class ResolveTest(SlackAdapterTestCase):
    def test_channel_id_resolves_to_its_name(self):
        a = self.make([(200, {}, {"ok": True, "channel": {"id": "C12345678",
                                                          "name": "team-room"}})])
        self.assertEqual(a.resolve("C12345678"), "team-room")

    def test_user_id_resolves_to_a_display_name(self):
        a = self.make([(200, {}, {"ok": True, "user": {
            "id": "U12345678", "name": "hbeaker",
            "profile": {"display_name": "Dr. Honeydew"}}})])
        self.assertEqual(a.resolve("U12345678"), "Dr. Honeydew")

    def test_channel_name_resolves_to_its_id_across_pages(self):
        a = self.make([
            (200, {}, {"ok": True, "channels": [{"id": "C_AAA", "name": "general"}],
                       "response_metadata": {"next_cursor": "p2"}}),
            (200, {}, {"ok": True, "channels": [{"id": "C_BBB", "name": "team-room"}]}),
        ])
        self.assertEqual(a.resolve("#team-room"), "C_BBB")

    def test_user_name_resolves_to_an_id(self):
        a = self.make([
            (200, {}, {"ok": True, "channels": []}),     # not a channel name
            (200, {}, {"ok": True, "members": [{"id": "U_ZZZ", "name": "hbeaker",
                                                "profile": {"display_name": "Beaker"}}]}),
        ])
        self.assertEqual(a.resolve("@Beaker"), "U_ZZZ")

    def test_an_unresolvable_ref_raises_naming_it(self):
        a = self.make([(200, {}, {"ok": True, "channels": []}),
                       (200, {}, {"ok": True, "members": []})])
        with self.assertRaises(LookupError) as ctx:
            a.resolve("nobody-here")
        self.assertIn("nobody-here", str(ctx.exception))


class RetrievableSnapshotTest(SlackAdapterTestCase):
    """The platform snapshot that lets core/parity tell a deletion from a lost message.

    Measured 2026-08-26 (R8's first live window): 342 of 507 oracle rows for one channel
    were no longer served by the API, and without this the differ read that as a read-path
    defect. The snapshot has to be as gap-free as polling is — a truncated snapshot would
    mark still-served messages 'unretrievable', which is exactly the direction that hides
    a real loss.
    """

    def test_returns_every_timestamp_the_platform_serves(self):
        a = self.make([history([slack_msg("3.0"), slack_msg("2.0"), slack_msg("1.0")])])
        self.assertEqual(a.retrievable_ts("C_ONE"), {"1.0", "2.0", "3.0"})

    def test_follows_pagination_to_the_end(self):
        a = self.make([
            history([slack_msg("9.0")], has_more=True, next_cursor="p2"),
            history([slack_msg("8.0")]),
        ])
        self.assertEqual(a.retrievable_ts("C_ONE"), {"8.0", "9.0"})

    def test_a_truncated_snapshot_raises_rather_than_under_reporting(self):
        """has_more with no cursor: silently returning page one would mark every later
        message 'deleted upstream' — a differ-blinding lie in the dangerous direction."""
        a = self.make([history([slack_msg("9.0")], has_more=True)])  # no next_cursor
        with self.assertRaises(self.mod.ApiError):
            a.retrievable_ts("C_ONE")

    def test_the_snapshot_bounds_the_window_it_was_asked_for(self):
        a = self.make([history([slack_msg("5.0")])])
        a.retrievable_ts("C_ONE", oldest="4.0", latest="6.0")
        _, form = self.http.requests[0]
        self.assertEqual((form["oldest"], form["latest"]), ("4.0", "6.0"))

    def test_the_snapshot_uses_a_read_method_only(self):
        a = self.make([history([slack_msg("1.0")])])
        a.retrievable_ts("C_ONE")
        url, _ = self.http.requests[0]
        self.assertTrue(url.endswith("/conversations.history"))
        self.assertIn("conversations.history", self.mod.READ_METHODS)



def replies_page(messages, has_more=False, next_cursor=None):
    return history(messages, has_more=has_more, next_cursor=next_cursor)


class ThreadRepliesTest(SlackAdapterTestCase):
    """replies() — the optional capability that lets in-thread answers close asks
    (ENH-30). conversations.history never serves a thread reply, so until this method
    existed every answer the agent posted in-thread was invisible to the engine."""

    def test_replies_pages_the_thread_and_never_returns_the_parent(self):
        a = self.make([
            replies_page([slack_msg("1.0", text="the ask", thread_ts="1.0",
                                    reply_count=2),
                          slack_msg("2.0", user="B_AGENT", text="answer",
                                    thread_ts="1.0")],
                         has_more=True, next_cursor="pg2"),
            replies_page([slack_msg("3.0", user="U_B", text="thanks",
                                    thread_ts="1.0")]),
        ])
        got = a.replies("C_ONE", "1.0")
        self.assertEqual([m["ts"] for m in got], ["2.0", "3.0"],
                         "the parent rides the first page of conversations.replies "
                         "and must be dropped; both pages must be followed")
        self.assertEqual({m["thread_id"] for m in got}, {"1.0"})
        self.assertEqual({m["channel_id"] for m in got}, {"C_ONE"})
        self.assertEqual(got[0]["sender_id"], "B_AGENT")
        urls = [u for u, _ in self.http.requests]
        self.assertTrue(all(u.endswith("/conversations.replies") for u in urls), urls)
        first = self.http.requests[0][1]
        self.assertEqual((first["channel"], first["ts"]), ("C_ONE", "1.0"))
        self.assertNotIn("oldest", first, "no oldest was asked for")
        self.assertEqual(self.http.requests[1][1]["cursor"], "pg2")
        self.assertIn("conversations.replies", self.mod.READ_METHODS)

    def test_oldest_is_passed_through_exclusively(self):
        a = self.make([replies_page([slack_msg("3.0", user="U_B", thread_ts="1.0")])])
        got = a.replies("C_ONE", "1.0", oldest="2.0")
        self.assertEqual(self.http.requests[0][1]["oldest"], "2.0")
        self.assertEqual([m["ts"] for m in got], ["3.0"])

    def test_a_truncated_thread_page_raises_instead_of_dropping_the_tail(self):
        a = self.make([replies_page([slack_msg("2.0", user="U_B", thread_ts="1.0")],
                                    has_more=True)])
        with self.assertRaises(self.mod.ApiError):
            a.replies("C_ONE", "1.0")

    def test_a_429_on_replies_carries_the_platforms_wait(self):
        a = self.make([(429, {"Retry-After": "7"}, {"ok": False, "error": "ratelimited"})])
        with self.assertRaises(RateLimited) as ctx:
            a.replies("C_ONE", "1.0")
        self.assertEqual(ctx.exception.retry_after, 7)

    def test_a_broadcast_reply_served_by_history_is_stored_as_a_thread_reply(self):
        """History DOES serve one kind of reply: one posted "also send to channel"
        (subtype thread_broadcast) arrives with thread_ts set. It must land with
        thread_id = the parent, so the sweep's store-side view of the thread includes
        it — and nothing in the poll path may strip thread replies (an earlier test
        here asserted the fixture, not the adapter: review F10)."""
        a = self.make([history([
            slack_msg("1.0", text="the ask", thread_ts="1.0", reply_count=1),
            slack_msg("3.0", user="U_A", text="bump (also sent to channel)",
                      thread_ts="1.0", subtype="thread_broadcast")])])
        got, _ = a.poll(None)
        self.assertEqual([(m["ts"], m["thread_id"]) for m in got],
                         [("1.0", None), ("3.0", "1.0")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
