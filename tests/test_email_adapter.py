"""Email (Outlook / any IMAP server) read-only adapter tests (ENH-26).

Email is the channel that most breaks the engine's founding assumptions, which is why
it exists as PHASE 5: message identity is a **Message-ID string** that no float() will
ever parse, threading rides the References/In-Reply-To headers rather than a parent ts,
and the sender controls the Date header, so nothing about a message's content is a
trustworthy ordering key. The properties pinned here:

* **identity is the Message-ID**, canonical bracketed form, never the Date header (two
  messages in one second must stay two rows; a re-delivered message must stay ONE row)
  and never the IMAP UID (a UIDVALIDITY reset reassigns every UID — a UID-keyed store
  would duplicate the whole mailbox after a reset);
* a message with NO Message-ID gets a **deterministic surrogate** derived from its raw
  bytes — a random or wall-clock surrogate would break re-poll idempotency (R9) by
  minting a new identity for the same message on every sighting;
* **ordering is mailbox UID order** — the order the mailbox assigned at arrival (RFC
  3501 UIDs are strictly ascending) — with an explicit rule that never touches
  float(ts) (impossible) and never trusts the sender-controlled Date header;
* **thread_id derives from References/In-Reply-To**: the FIRST id in References is the
  thread root (RFC 5322 lists ancestors oldest-first), which gives every reply the
  ROOT's identity — the same alignment Slack's thread_ts gives core for free;
  In-Reply-To (immediate parent) is the fallback for clients that send only it;
* read-only in LAYERS: no send surface exists, and the command funnel default-denies
  everything outside the read allowlist before a byte reaches the wire — including the
  two writes hidden inside "reads": a mailbox SELECT that is not read-only (EXAMINE)
  lets the server mutate flags, and a FETCH without BODY.PEEK sets \\Seen;
* the **store and parity paths work end to end with non-numeric identity**: the store
  keys rows on the Message-ID, and core/parity classifies divergence by served-set
  membership without ever needing the ids to be orderable.

All I/O is faked via the injectable client factory; nothing here touches a server.
"""
import email.policy
import email.utils
import importlib.util
import json
import sys
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import discover_adapters, load_adapter_class  # noqa: E402
from core.parity import UNRETRIEVABLE, compare, snapshot_declaration  # noqa: E402
from core.store import Store  # noqa: E402

ADAPTER_PY = ROOT / "channels" / "email" / "adapter.py"


def _load_module():
    """The module itself (not just the Adapter class) — the tests pin the transport
    guard exceptions, which load_adapter_class deliberately does not expose."""
    spec = importlib.util.spec_from_file_location("email_adapter_under_test",
                                                  ADAPTER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def mail(mid="<a1@example.com>", frm="Anton Alexander <anton@example.com>",
         subject="hello", text="body text",
         date="Mon, 24 Aug 2026 10:00:00 +0000",
         references=None, in_reply_to=None, attachments=(), html=None):
    """Raw RFC 5322 bytes, as an IMAP FETCH would serve them."""
    m = EmailMessage()
    if mid is not None:
        m["Message-ID"] = mid
    if frm is not None:
        m["From"] = frm
    m["Subject"] = subject
    m["Date"] = date
    if references:
        m["References"] = references
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
    if html is not None:
        m.set_content(html, subtype="html")
    else:
        m.set_content(text)
    for (payload, maintype, subtype, filename) in attachments:
        m.add_attachment(payload, maintype=maintype, subtype=subtype,
                         filename=filename)
    return m.as_bytes()


class FakeImap:
    """In-memory RFC-3501-shaped server. Records every command that reached it — the
    read-only tests assert on `calls` staying clean — and implements the one wire
    quirk the adapter must survive: a `UID SEARCH UID n:*` where n exceeds the newest
    UID still returns that newest UID (RFC 3501 range endpoints swap, and `*` is
    always included)."""

    def __init__(self, mailboxes=None, uidvalidity=None, fail_login=None):
        self.mailboxes = {k: dict(v) for k, v in (mailboxes or {}).items()}
        self.uidvalidity = dict(uidvalidity or {})
        for name in self.mailboxes:
            self.uidvalidity.setdefault(name, 1)
        self.fail_login = fail_login
        self.calls = []          # every command that reached the "wire"
        self.seen_set = []       # uids a non-PEEK fetch would have flagged \Seen
        self.selected = None

    def login(self, user, password):
        self.calls.append(("login", user))
        if self.fail_login is not None:
            raise self.fail_login
        return ("OK", [b"Logged in"])

    def select(self, mailbox, readonly=False):
        self.calls.append(("select", mailbox, readonly))
        if mailbox not in self.mailboxes:
            return ("NO", [b"nonexistent mailbox"])
        self.selected = mailbox
        return ("OK", [str(len(self.mailboxes[mailbox])).encode()])

    def response(self, key):
        if key == "UIDVALIDITY" and self.selected is not None:
            return (key, [str(self.uidvalidity[self.selected]).encode()])
        return (key, [None])

    def uid(self, command, *args):
        self.calls.append(("uid", command) + args)
        box = self.mailboxes[self.selected]
        if command.upper() == "SEARCH":
            start = int(args[-1].split(":")[0])
            uids = sorted(box)
            hits = [u for u in uids if u >= start]
            if not hits and uids:
                hits = [uids[-1]]        # the n:* quirk
            return ("OK", [" ".join(str(u) for u in hits).encode()])
        if command.upper() == "FETCH":
            uid = int(args[0])
            if "PEEK" not in args[-1].upper():
                self.seen_set.append(uid)
            if uid not in box:
                return ("OK", [None])
            head = f"1 (UID {uid} BODY[] {{{len(box[uid])}}}".encode()
            return ("OK", [(head, box[uid]), b")"])
        raise AssertionError(f"unexpected uid subcommand {command!r}")

    def noop(self):
        self.calls.append(("noop",))
        return ("OK", [b"NOOP"])

    def logout(self):
        self.calls.append(("logout",))
        return ("BYE", [b"bye"])

    # write surfaces a broken funnel would reach — they must never be called
    def append(self, *a, **k):
        raise AssertionError("APPEND reached the wire on a read-only adapter")

    def store(self, *a, **k):
        raise AssertionError("STORE reached the wire on a read-only adapter")

    def expunge(self, *a, **k):
        raise AssertionError("EXPUNGE reached the wire on a read-only adapter")


class EmailAdapterTestCase(unittest.TestCase):
    def setUp(self):
        self.mod = _load_module()

    def make(self, mailboxes=None, uidvalidity=None, channels="INBOX",
             fail_login=None, imap_error=None):
        self.fake = FakeImap(mailboxes or {"INBOX": {}}, uidvalidity,
                             fail_login=fail_login)
        factory = (lambda host: (_ for _ in ()).throw(imap_error)) if imap_error \
            else (lambda host: self.fake)
        return self.mod.Adapter(
            auth={"host": "imap.example.com", "username": "watcher@example.com",
                  "password": "pw", "channels": channels},
            imap=factory)


class ContractSurfaceTest(EmailAdapterTestCase):
    def test_email_is_discovered_from_the_shipped_tree(self):
        """R11 discovery is the landing mechanism — a dir-drop, zero core/ changes."""
        self.assertIn("email", discover_adapters(ROOT / "channels"))
        cls = load_adapter_class(ROOT / "channels", "email")
        self.assertTrue(callable(cls))

    def test_capabilities_answer_every_contract_key_and_are_honest(self):
        caps = self.make().capabilities()
        for k in ("read", "history", "search", "send", "react", "threads"):
            self.assertIn(k, caps)
        self.assertTrue(caps["read"])
        self.assertTrue(caps["history"],
                        "IMAP re-reads freely — claiming no history would deny parity "
                        "the snapshot this platform CAN supply")
        self.assertTrue(caps["threads"])
        self.assertFalse(caps["send"],
                         "a read-only adapter advertising send invites the outbox to "
                         "drive a path that must not exist")

    def test_no_callable_send_exists_at_any_public_surface(self):
        a = self.make()
        self.assertIsNone(getattr(a, "send", None))
        self.assertIsNone(getattr(a, "read_back", None),
                          "read_back is proof-of-delivery; it implies a delivery path")
        for name in dir(a):
            if name.startswith("_"):
                continue
            for banned in ("send", "post", "write", "publish", "react", "append"):
                self.assertNotIn(banned, name.lower(),
                                 f"public attribute {name!r} looks like a send surface")

    def test_missing_auth_keys_refuse_at_construction(self):
        for missing in ("host", "username", "password", "channels"):
            auth = {"host": "h", "username": "u", "password": "p",
                    "channels": "INBOX"}
            del auth[missing]
            with self.assertRaises(ValueError, msg=missing) as ctx:
                self.mod.Adapter(auth=auth)
            self.assertIn(missing, str(ctx.exception))

    def test_the_watch_set_is_exposed_for_the_doctor_cross_check(self):
        a = self.make(channels="INBOX, Archive")
        self.assertEqual(a.channels, ("INBOX", "Archive"))


class ReadOnlyTransportTest(EmailAdapterTestCase):
    """Deny-by-default at the layer every command must pass through. IMAP hides writes
    inside reads: SELECT without read-only lets the server mutate flags, and a FETCH
    without BODY.PEEK sets \\Seen — both are refused at the funnel, not avoided by
    convention."""

    def test_write_commands_are_refused_before_any_io(self):
        a = self.make()
        for command in ("append", "store", "expunge", "create", "delete",
                        "copy", "setacl", "authenticate"):
            with self.assertRaises(self.mod.ReadOnlyViolation, msg=command):
                a._exec(self.fake, command, "INBOX")
        self.assertEqual(self.fake.calls, [],
                         "a refused command still reached the wire — the guard runs "
                         "after I/O, which is no guard at all")

    def test_a_writing_uid_subcommand_is_refused_before_any_io(self):
        a = self.make()
        for sub in ("STORE", "COPY", "EXPUNGE", "MOVE"):
            with self.assertRaises(self.mod.ReadOnlyViolation, msg=sub):
                a._exec(self.fake, "uid", sub, "1", "+FLAGS", r"(\Deleted)")
        self.assertEqual(self.fake.calls, [])

    def test_a_fetch_without_peek_is_refused_before_any_io(self):
        """BODY[] (no PEEK) sets \\Seen on the server — a poll that marks the
        operator's unread mail read is a write wearing a read's name."""
        a = self.make()
        with self.assertRaises(self.mod.ReadOnlyViolation):
            a._exec(self.fake, "uid", "FETCH", "1", "(BODY[])")
        self.assertEqual(self.fake.calls, [])

    def test_every_mailbox_select_is_read_only(self):
        """EXAMINE, not SELECT: a writable selection lets the server set \\Recent and
        recent-clearing side effects — the funnel forces readonly no matter what the
        caller passed."""
        a = self.make({"INBOX": {1: mail()}})
        a.poll(None)
        selects = [c for c in self.fake.calls if c[0] == "select"]
        self.assertTrue(selects)
        for c in selects:
            self.assertTrue(c[2], f"mailbox {c[1]!r} was selected WRITABLE")

    def test_polling_never_sets_seen(self):
        a = self.make({"INBOX": {1: mail(), 2: mail(mid="<a2@example.com>")}})
        a.poll(None)
        self.assertEqual(self.fake.seen_set, [],
                         "poll() flagged mail \\Seen — a read that mutates the "
                         "operator's mailbox state")


class IdentityTest(EmailAdapterTestCase):
    """Identity is the Message-ID — the acceptance property everything else builds on."""

    def test_ts_is_the_bracketed_message_id_and_never_parses_as_a_float(self):
        a = self.make({"INBOX": {1: mail(mid="<a1@example.com>")}})
        msgs, _ = a.poll(None)
        self.assertEqual(msgs[0]["ts"], "<a1@example.com>")
        with self.assertRaises(ValueError):
            float(msgs[0]["ts"])

    def test_two_messages_in_the_same_second_stay_two_rows(self):
        """The store keys rows (channel_type, channel_id, ts) and Date is whole
        seconds — a Date-based ts would merge burst messages into one row."""
        same = "Mon, 24 Aug 2026 10:00:00 +0000"
        a = self.make({"INBOX": {1: mail(mid="<a1@example.com>", date=same),
                                 2: mail(mid="<a2@example.com>", date=same)}})
        msgs, _ = a.poll(None)
        self.assertNotEqual(msgs[0]["ts"], msgs[1]["ts"])
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "m.db")
            try:
                store.upsert_messages(msgs)
                self.assertEqual(store.count("INBOX"), 2)
            finally:
                store.close()

    def test_identity_survives_a_uid_reassignment(self):
        """THE reason identity is the Message-ID and not the UID: a UIDVALIDITY reset
        reassigns every UID, and a UID-keyed store would re-ingest the whole mailbox
        as new rows — silent duplication of history. Message-ID keys make the re-read
        an idempotent no-op."""
        raw = mail(mid="<stable@example.com>")
        a = self.make({"INBOX": {5: raw}}, uidvalidity={"INBOX": 1})
        msgs, cursor = a.poll(None)
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "m.db")
            try:
                store.upsert_messages(msgs)
                # the server resets: same message, new validity, new uid
                self.fake.uidvalidity["INBOX"] = 2
                self.fake.mailboxes["INBOX"] = {1: raw}
                again, _ = a.poll(cursor)
                self.assertEqual([m["ts"] for m in again],
                                 ["<stable@example.com>"],
                                 "a UIDVALIDITY reset must re-serve history "
                                 "(duplicates, never loss)")
                store.upsert_messages(again)
                self.assertEqual(store.count("INBOX"), 1,
                                 "the re-served message duplicated — identity leaked "
                                 "from the Message-ID to the UID")
            finally:
                store.close()

    def test_a_missing_message_id_gets_a_deterministic_surrogate(self):
        """RFC 5322 makes Message-ID a SHOULD, not a MUST. A surrogate derived from
        anything but the message bytes (a random uuid, the wall clock, the UID) mints
        a NEW identity per sighting, so every re-poll duplicates the row (R9)."""
        raw = mail(mid=None)
        a = self.make({"INBOX": {1: raw}})
        first, _ = a.poll(None)
        again, _ = a.poll(None)
        self.assertEqual(first[0]["ts"], again[0]["ts"],
                         "the surrogate identity changed between sightings of the "
                         "same message — re-polls will duplicate rows")
        self.assertTrue(first[0]["ts"].startswith("<"),
                        "the surrogate must live in the same bracketed namespace as "
                        "real Message-IDs")
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "m.db")
            try:
                store.upsert_messages(first)
                store.upsert_messages(again)
                self.assertEqual(store.count("INBOX"), 1)
            finally:
                store.close()

    def test_normalized_messages_meet_the_pinned_contract(self):
        a = self.make({"INBOX": {1: mail()}})
        msgs, _ = a.poll(None)
        m = msgs[0]
        Store.validate(m)
        self.assertEqual(m["channel_type"], "email")
        self.assertEqual(m["channel_id"], "INBOX")
        self.assertEqual(m["sender_id"], "anton@example.com")
        self.assertEqual(m["sender_name"], "Anton Alexander")
        self.assertIsInstance(m["raw"], str,
                              "raw must be a JSON string — the store binds it "
                              "straight into sqlite")
        self.assertEqual(json.loads(m["raw"])["uid"], 1,
                         "raw must keep the retrieval handle (mailbox/uid/validity) — "
                         "audit needs it next to the content")

    def test_a_senderless_message_still_meets_the_required_fields(self):
        a = self.make({"INBOX": {1: mail(frm=None)}})
        msgs, _ = a.poll(None)
        self.assertIsNotNone(msgs[0]["sender_id"])
        Store.validate(msgs[0])


class OrderingTest(EmailAdapterTestCase):
    """The acceptance's explicit ordering rule: mailbox UID order (arrival order at
    the mailbox, RFC 3501 strictly ascending) — never float(ts) (impossible on a
    Message-ID), never the sender-controlled Date header, never lexicographic id."""

    def test_poll_orders_by_mailbox_uid_never_by_date_or_id_text(self):
        # Date order and lexicographic Message-ID order BOTH contradict UID order,
        # so a fallback to either fails this test.
        boxes = {"INBOX": {
            3: mail(mid="<zz-first-arrived@example.com>",
                    date="Wed, 26 Aug 2026 09:00:00 +0000"),
            7: mail(mid="<aa-arrived-later@example.com>",
                    date="Mon, 24 Aug 2026 09:00:00 +0000"),
        }}
        a = self.make(boxes)
        msgs, _ = a.poll(None)
        self.assertEqual([m["ts"] for m in msgs],
                         ["<zz-first-arrived@example.com>",
                          "<aa-arrived-later@example.com>"])

    def test_a_search_served_out_of_order_is_still_returned_in_uid_order(self):
        class ShuffledImap(FakeImap):
            def uid(self, command, *args):
                typ, data = super().uid(command, *args)
                if command.upper() == "SEARCH" and data and data[0]:
                    uids = data[0].split()
                    return typ, [b" ".join(reversed(uids))]
                return typ, data

        raws = {i: mail(mid=f"<m{i}@example.com>") for i in (1, 2, 3)}
        self.fake = ShuffledImap({"INBOX": raws})
        a = self.mod.Adapter(
            auth={"host": "h", "username": "u", "password": "p",
                  "channels": "INBOX"},
            imap=lambda host: self.fake)
        msgs, _ = a.poll(None)
        self.assertEqual([m["ts"] for m in msgs],
                         ["<m1@example.com>", "<m2@example.com>",
                          "<m3@example.com>"])


class CursorTest(EmailAdapterTestCase):
    def test_poll_is_gap_free_and_idempotent_from_the_same_cursor(self):
        a = self.make({"INBOX": {1: mail(mid="<a1@x>"), 2: mail(mid="<a2@x>")}})
        first, cursor = a.poll(None)
        self.assertEqual(len(first), 2)
        # a crash before the cursor commit re-polls the same window: duplicates,
        # never loss (the store absorbs them, R9)
        again, cursor2 = a.poll(None)
        self.assertEqual([m["ts"] for m in first], [m["ts"] for m in again])
        self.assertEqual(cursor, cursor2)

    def test_a_poll_at_the_committed_cursor_returns_only_news(self):
        a = self.make({"INBOX": {1: mail(mid="<a1@x>")}})
        _, cursor = a.poll(None)
        self.fake.mailboxes["INBOX"][2] = mail(mid="<a2@x>")
        news, cursor2 = a.poll(cursor)
        self.assertEqual([m["ts"] for m in news], ["<a2@x>"])
        self.assertNotEqual(cursor, cursor2)

    def test_the_uid_star_quirk_does_not_reingest_the_newest_message(self):
        """UID SEARCH UID n:* returns the newest message even when n exceeds it (RFC
        3501 range semantics) — the adapter must filter below the cursor or every
        idle poll re-serves the tip forever."""
        a = self.make({"INBOX": {1: mail(mid="<a1@x>")}})
        _, cursor = a.poll(None)
        news, cursor2 = a.poll(cursor)
        self.assertEqual(news, [])
        self.assertEqual(cursor, cursor2)

    def test_a_junk_cursor_is_refused_before_any_io(self):
        """A cursor the adapter did not mint means state corruption somewhere;
        guessing a window from it would silently re-shape what gets polled."""
        a = self.make()
        for junk in ("not-json", '["list"]', '{"INBOX": "no-colon"}',
                     '{"INBOX": "1:2:3"}'):
            with self.assertRaises(ValueError, msg=junk):
                a.poll(junk)
        self.assertEqual(self.fake.calls, [],
                         "the refusal must happen before any I/O")

    def test_the_cursor_carries_uidvalidity_and_a_reset_restarts_the_read(self):
        a = self.make({"INBOX": {9: mail(mid="<a1@x>")}}, uidvalidity={"INBOX": 41})
        _, cursor = a.poll(None)
        self.assertEqual(json.loads(cursor)["INBOX"], "41:9")
        # validity changes: the old uid 9 means nothing now — restart from the
        # beginning rather than trust a number from a dead uid-space
        self.fake.uidvalidity["INBOX"] = 42
        self.fake.mailboxes["INBOX"] = {1: mail(mid="<a1@x>"),
                                        2: mail(mid="<a2@x>")}
        news, cursor2 = a.poll(cursor)
        self.assertEqual([m["ts"] for m in news], ["<a1@x>", "<a2@x>"],
                         "a UIDVALIDITY reset must re-read the mailbox — trusting "
                         "the stale uid silently skips everything below it")
        self.assertEqual(json.loads(cursor2)["INBOX"], "42:2")

    def test_each_mailbox_keeps_its_own_offset(self):
        a = self.make({"INBOX": {1: mail(mid="<i1@x>")},
                       "Archive": {8: mail(mid="<r8@x>")}},
                      channels="INBOX,Archive")
        msgs, cursor = a.poll(None)
        self.assertEqual({m["channel_id"] for m in msgs}, {"INBOX", "Archive"})
        offsets = json.loads(cursor)
        self.assertEqual(offsets["INBOX"].split(":")[1], "1")
        self.assertEqual(offsets["Archive"].split(":")[1], "8")


class ThreadingTest(EmailAdapterTestCase):
    """thread_id derives from References/In-Reply-To (the acceptance's threading
    clause): the FIRST References id is the thread ROOT — every reply carries the
    root's identity, the alignment Slack's thread_ts gives core for free."""

    def test_a_reply_carries_the_thread_root_from_references(self):
        boxes = {"INBOX": {
            1: mail(mid="<root@x>"),
            2: mail(mid="<r1@x>", references="<root@x>", in_reply_to="<root@x>"),
            3: mail(mid="<r2@x>", references="<root@x> <r1@x>",
                    in_reply_to="<r1@x>"),
        }}
        msgs, _ = self.make(boxes).poll(None)
        by_id = {m["ts"]: m for m in msgs}
        self.assertIsNone(by_id["<root@x>"]["thread_id"],
                          "the contract wants null for top-level messages")
        self.assertEqual(by_id["<r1@x>"]["thread_id"], "<root@x>")
        self.assertEqual(by_id["<r2@x>"]["thread_id"], "<root@x>",
                         "References lists ancestors oldest-first (RFC 5322): the "
                         "FIRST id is the root, and a deep reply must carry the "
                         "root, not its immediate parent")
        # the alignment core relies on: a reply's thread_id IS a stored row's ts
        self.assertIn(by_id["<r2@x>"]["thread_id"], by_id)

    def test_in_reply_to_is_the_fallback_when_references_is_absent(self):
        boxes = {"INBOX": {
            1: mail(mid="<root@x>"),
            2: mail(mid="<r1@x>", in_reply_to="<root@x>"),
        }}
        msgs, _ = self.make(boxes).poll(None)
        self.assertEqual(msgs[1]["thread_id"], "<root@x>")

    def test_a_self_reference_does_not_create_a_self_thread(self):
        boxes = {"INBOX": {1: mail(mid="<loop@x>", references="<loop@x>")}}
        msgs, _ = self.make(boxes).poll(None)
        self.assertIsNone(msgs[0]["thread_id"])


class ContentTest(EmailAdapterTestCase):
    def test_plain_text_is_preferred_and_html_only_is_preserved(self):
        boxes = {"INBOX": {1: mail(mid="<t@x>", text="the plain body"),
                           2: mail(mid="<h@x>", html="<p>rendered</p>")}}
        msgs, _ = self.make(boxes).poll(None)
        self.assertIn("the plain body", msgs[0]["text"])
        self.assertIn("<p>rendered</p>", msgs[1]["text"],
                      "an HTML-only message must keep its markup (the contract "
                      "preserves platform markup) — an empty text row classifies "
                      "as an empty STATEMENT and is forgotten")

    def test_an_attachment_is_content_with_no_minted_url(self):
        boxes = {"INBOX": {1: mail(
            mid="<att@x>", text="see attached",
            attachments=((b"\x89PNG", "image", "png", "shot.png"),
                         (b"%PDF", "application", "pdf", "report.pdf")))}}
        msgs, _ = self.make(boxes).poll(None)
        atts = msgs[0]["attachments"]
        self.assertEqual(atts[0], {"kind": "image", "name": "shot.png",
                                   "mimetype": "image/png", "url": None})
        self.assertEqual(atts[1]["kind"], "file")
        self.assertEqual(atts[1]["mimetype"], "application/pdf")
        self.assertIsNone(atts[1]["url"],
                          "IMAP has no URLs — the uid in raw is the retrieval "
                          "handle; a minted url would be a lie the store persists")

    def test_a_plain_message_carries_a_known_empty_attachment_list(self):
        msgs, _ = self.make({"INBOX": {1: mail()}}).poll(None)
        self.assertEqual(msgs[0]["attachments"], [])


class ResolveTest(EmailAdapterTestCase):
    def test_a_mailbox_name_resolves_case_insensitively_to_its_canonical_form(self):
        a = self.make(channels="INBOX,Archive")
        self.assertEqual(a.resolve("inbox"), "INBOX")
        self.assertEqual(a.resolve("archive"), "Archive")

    def test_an_address_is_its_own_identity(self):
        self.assertEqual(self.make().resolve("anton@example.com"),
                         "anton@example.com")

    def test_an_unresolvable_ref_raises_naming_it(self):
        with self.assertRaises(LookupError) as ctx:
            self.make().resolve("Drafts")
        self.assertIn("Drafts", str(ctx.exception))


class HealthTest(EmailAdapterTestCase):
    def test_health_passes_on_login_ok(self):
        h = self.make().health()
        self.assertTrue(h["reachable"])
        self.assertTrue(h["auth_ok"])

    def test_health_fails_on_a_rejected_login(self):
        """Contract rule 5: a health check that can only pass is a defect."""
        import imaplib
        h = self.make(fail_login=imaplib.IMAP4.error("LOGIN failed")).health()
        self.assertTrue(h["reachable"])
        self.assertFalse(h["auth_ok"])
        self.assertIn("LOGIN failed", h["detail"])

    def test_health_fails_when_the_server_is_unreachable(self):
        h = self.make(imap_error=OSError("no route to host")).health()
        self.assertFalse(h["reachable"])
        self.assertFalse(h["auth_ok"])

    def test_health_never_reads_a_mailbox(self):
        self.make().health()
        self.assertEqual([c[0] for c in self.fake.calls
                          if c[0] not in ("login", "noop", "logout")], [],
                         "liveness must stay cheap — no mailbox selection, no "
                         "message reads")


class SnapshotTest(EmailAdapterTestCase):
    """IMAP is the anti-Telegram: it CAN answer 'what do you still serve?', so the
    snapshot capability exists — with the SAME identity derivation as poll(), or a
    missing-Message-ID row would read as both UNRETRIEVABLE and ENGINE_ONLY."""

    def test_the_snapshot_capability_exists_and_declares_nothing(self):
        a = self.make()
        self.assertIsNone(snapshot_declaration(a, "email"))

    def test_the_snapshot_is_the_message_id_set_the_store_would_hold(self):
        boxes = {"INBOX": {1: mail(mid="<a1@x>"), 2: mail(mid=None)}}
        a = self.make(boxes)
        msgs, _ = a.poll(None)
        self.assertEqual(a.retrievable_ts("INBOX"),
                         {m["ts"] for m in msgs},
                         "the snapshot and the store derive identity differently — "
                         "parity would classify the same row as missing AND extra")

    def test_a_bounded_snapshot_is_refused_not_truncated(self):
        """Message-IDs do not form a range; guessing a bound would under-report,
        and an under-reported snapshot marks still-served messages 'deleted
        upstream' — the one direction that hides a real loss."""
        a = self.make({"INBOX": {1: mail()}})
        with self.assertRaises(ValueError):
            a.retrievable_ts("INBOX", oldest="<a1@x>")
        with self.assertRaises(ValueError):
            a.retrievable_ts("INBOX", latest="<a1@x>")

    def test_the_snapshot_read_is_read_only_too(self):
        a = self.make({"INBOX": {1: mail()}})
        a.retrievable_ts("INBOX")
        self.assertEqual(self.fake.seen_set, [])
        for c in self.fake.calls:
            if c[0] == "select":
                self.assertTrue(c[2])


class EndToEndNonNumericIdentityTest(EmailAdapterTestCase):
    """THE acceptance test: the store and parity paths work end to end with an
    identity no float() will parse — poll, ingest, snapshot, classify, verdict."""

    def _pipeline(self):
        boxes = {"INBOX": {i: mail(mid=f"<m{i}@example.com>") for i in (1, 2, 3)}}
        a = self.make(boxes)
        msgs, cursor = a.poll(None)
        store = Store(self.db)
        store.upsert_messages(msgs)
        again, _ = a.poll(None)          # crash-before-commit re-poll
        store.upsert_messages(again)
        return a, store

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "m.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_ingest_is_idempotent_and_parity_goes_green_without_ordering(self):
        a, store = self._pipeline()
        try:
            self.assertEqual(store.count("INBOX"), 3)
            report = compare(oracle_ts=store.timestamps("INBOX"),
                             candidate_ts=store.timestamps("INBOX"),
                             channel="INBOX",
                             served_ts=a.retrievable_ts("INBOX"))
            self.assertTrue(report.ok)
            self.assertFalse(report.orderable,
                             "Message-IDs must flow through parity as NON-orderable "
                             "identities — if this reads True, something coerced "
                             "them to floats")
        finally:
            store.close()

    def test_a_deleted_email_classifies_unretrievable_not_engine_lost(self):
        a, store = self._pipeline()
        try:
            oracle = store.timestamps("INBOX")
            del self.fake.mailboxes["INBOX"][2]      # deleted upstream
            fresh = Store(Path(self.tmp.name) / "fresh.db")
            try:
                msgs, _ = a.poll(None)
                fresh.upsert_messages(msgs)
                report = compare(oracle_ts=oracle,
                                 candidate_ts=fresh.timestamps("INBOX"),
                                 channel="INBOX",
                                 served_ts=a.retrievable_ts("INBOX"),
                                 accept=(UNRETRIEVABLE,))
                self.assertTrue(report.ok)
                self.assertEqual(report.classified["<m2@example.com>"],
                                 UNRETRIEVABLE)
            finally:
                fresh.close()
        finally:
            store.close()

    def test_a_genuinely_lost_email_still_fails_parity(self):
        """The classification must not have gone soft: a message the server still
        serves and the store lacks is ENGINE_LOST, unwaivable, non-numeric id or
        not."""
        a, store = self._pipeline()
        try:
            oracle = store.timestamps("INBOX")
            gappy = Store(Path(self.tmp.name) / "gappy.db")
            try:
                msgs, _ = a.poll(None)
                gappy.upsert_messages([m for m in msgs
                                       if m["ts"] != "<m2@example.com>"])
                report = compare(oracle_ts=oracle,
                                 candidate_ts=gappy.timestamps("INBOX"),
                                 channel="INBOX",
                                 served_ts=a.retrievable_ts("INBOX"),
                                 accept=(UNRETRIEVABLE,))
                self.assertFalse(report.ok)
                self.assertEqual(report.classified["<m2@example.com>"],
                                 "ENGINE_LOST")
                self.assertIn("<m2@example.com>",
                              report.panel()["engine_lost_sample"])
            finally:
                gappy.close()
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
