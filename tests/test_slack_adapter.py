"""Slack read-only adapter tests (ENH-18; feeds gates G1/R8/R17).

The adapter under test is the first REAL platform adapter, and it lands with a property
the incumbent never had: **no send path exists at any layer**. The operator authorized
the workspace token READ-ONLY, so "read-only" must be a tested invariant, not a promise:

* the adapter exposes no callable `send` (nothing for an outbox to even mis-drive);
* the transport itself default-denies — any Web API method outside the read allowlist
  is refused BEFORE a byte reaches the wire, so a future helper cannot smuggle a write
  through the generic `_api` primitive;
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
        return self.mod.Adapter(auth={"token": "tok", "channels": channels},
                                http=self.http, **kwargs)


class ContractSurfaceTest(SlackAdapterTestCase):
    def test_slack_is_discovered_from_the_shipped_tree(self):
        """R11 discovery is the landing mechanism — a dir-drop, zero core/ changes."""
        self.assertIn("slack", discover_adapters(ROOT / "channels"))
        cls = load_adapter_class(ROOT / "channels", "slack")
        self.assertTrue(callable(cls))

    def test_capabilities_answer_every_contract_key_and_send_is_false(self):
        caps = self.make().capabilities()
        for k in ("read", "history", "search", "send", "react", "threads"):
            self.assertIn(k, caps)
        self.assertTrue(caps["read"])
        self.assertTrue(caps["history"])
        self.assertFalse(caps["send"],
                         "a read-only adapter advertising send invites the outbox to "
                         "drive a path that must not exist")

    def test_no_callable_send_exists_at_any_public_surface(self):
        """THE acceptance property: the operator authorized the token read-only, so
        there is nothing named like a send for any caller — outbox included — to find."""
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
        """Half-configured is the worst state to discover during an incident — the
        same load-time-refusal rule core/config.py applies to env vars."""
        with self.assertRaises(ValueError) as ctx:
            self.mod.Adapter(auth={"channels": "C_ONE"})
        self.assertIn("token", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            self.mod.Adapter(auth={"token": "tok"})
        self.assertIn("channels", str(ctx.exception))


class ReadOnlyTransportTest(SlackAdapterTestCase):
    """Deny-by-default at the layer every request must pass through. Without this, the
    no-send property above is one convenience wrapper away from being lost."""

    WRITE_METHODS = ("chat.postMessage", "chat.update", "chat.delete", "chat.meMessage",
                     "reactions.add", "files.upload", "conversations.join")

    def test_write_methods_are_refused_before_any_io(self):
        a = self.make()
        for method in self.WRITE_METHODS:
            with self.assertRaises(self.mod.ReadOnlyViolation, msg=method):
                a._api(method, channel="C_ONE", text="never")
        self.assertEqual(self.http.requests, [],
                         "a refused method still reached the wire — the guard runs "
                         "after I/O, which is no guard at all")

    def test_read_methods_pass_the_guard(self):
        a = self.make([(200, {}, {"ok": True})])
        a._api("auth.test")
        self.assertEqual(len(self.http.requests), 1)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
