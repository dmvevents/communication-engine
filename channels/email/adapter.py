"""channels/email — the read-only IMAP adapter (ENH-26; Outlook / any RFC-3501 server).

PHASE 5's platform, and the one that most breaks the engine's founding assumptions.
Slack and Telegram both hand core an identity that is also an ordering key (a float ts,
a monotonic message_id). Email refuses: identity is a **Message-ID string** no float()
will ever parse, ordering belongs to the mailbox rather than the message, and threading
rides headers instead of a parent ts. Three explicit rules replace the assumptions:

* **Identity is the Message-ID** (canonical bracketed form, RFC 5322). Not the Date
  header — the sender writes it, it is whole seconds, and burst messages would merge
  into one store row. Not the IMAP UID — a UIDVALIDITY reset reassigns every UID, and a
  UID-keyed store would re-ingest the whole mailbox as duplicates. A message with no
  Message-ID (RFC 5322 makes it a SHOULD) gets a DETERMINISTIC surrogate derived from
  its raw bytes: any other source (uuid, clock, UID) mints a new identity per sighting
  and breaks re-poll idempotency (R9). core/parity classifies these ids by served-set
  membership without ever ordering them (its ENH-26 non-orderable mode).

* **Ordering is mailbox UID order** — the order the mailbox assigned at arrival, which
  RFC 3501 guarantees strictly ascending — never float(ts) (impossible here) and never
  the sender-controlled Date. The cursor is a JSON object mapping mailbox ->
  "uidvalidity:uid" (channels/slack's per-channel-offsets shape): UIDs only mean
  anything within one UIDVALIDITY epoch, so the epoch rides the cursor, and a reset
  restarts the read from the beginning — re-served history lands as duplicates the
  Message-ID-keyed store absorbs, which is the direction that never loses. One IMAP
  wire quirk matters for the idle loop: `UID SEARCH UID n:*` returns the newest message
  even when n exceeds it (range endpoints swap and `*` is always included), so results
  are filtered back to the cursor or every idle poll re-serves the tip.

* **thread_id derives from References/In-Reply-To**: the FIRST id in References is the
  thread ROOT (RFC 5322 lists ancestors oldest-first), so every reply carries the
  root's identity — the same alignment Slack's thread_ts gives core for free.
  In-Reply-To (the immediate parent) is the fallback for clients that send only it; a
  mid-thread reply from such a client carries its parent rather than the root, an
  honest degradation of the client's own data, never a guess.

Read-only is enforced in layers, not promised in prose: no send, read_back, or delivery
primitive exists on this class, and `_exec` — the one funnel every command passes
through — default-DENIES anything outside READ_COMMANDS before a byte reaches the wire.
IMAP hides writes inside reads, so the funnel also forces both of them shut: every
mailbox selection is read-only (EXAMINE — a writable SELECT lets the server clear
\\Recent), and every fetch must use BODY.PEEK (a plain BODY fetch marks the operator's
unread mail \\Seen — a write wearing a read's name).

Unlike Telegram (which cannot re-read anything it acknowledged), IMAP is a full history
platform: `retrievable_ts` exists and answers "what do you still serve?" with the same
identity derivation as poll() — byte-identical on purpose, or a missing-Message-ID row
would read as both UNRETRIEVABLE and ENGINE_ONLY in the same parity run. Bounded
snapshots are refused: Message-IDs do not form a range, and a truncated snapshot marks
still-served messages "deleted upstream", the one direction that hides a real loss.

Auth arrives as `env:NAME` references via config (`auth: {"host", "username",
"password", "channels"}`; channels is a comma-separated mailbox list, e.g. "INBOX").
For Outlook / M365 the host is outlook.office365.com; note Microsoft requires OAuth2
(XOAUTH2) there — an app password or a provider that still allows IMAP LOGIN is the
supported path for this adapter, and that constraint is recorded in README.md rather
than worked around with a token dance this repo cannot test. There is no 429 dialect on
IMAP (contract rule 3 is vacuous here): servers refuse at LOGIN or throttle the socket,
both of which surface through health() rather than core/ratelimit.
"""
from __future__ import annotations

import email
import email.policy
import email.utils
import hashlib
import imaplib
import json
import re

# Deny-by-default: everything this adapter is allowed to call, exhaustively. Extending
# the adapter means extending this list IN THE SAME DIFF as the test that uses it.
READ_COMMANDS = frozenset({
    "login",
    "select",     # forced read-only (EXAMINE) in _exec, whatever the caller passed
    "uid",        # subcommand-checked below: SEARCH and FETCH only
    "noop",
    "logout",
})
UID_READ_SUBCOMMANDS = frozenset({"SEARCH", "FETCH"})

# A Message-ID token: RFC 5322 angle-bracket form. Applied with findall, so a
# References header yields the ancestor chain in written (oldest-first) order.
_MSGID = re.compile(r"<[^<>\s]+>")

# The adapter-private cursor entry for one mailbox: "uidvalidity:last-ingested-uid".
_MARK = re.compile(r"\d+:\d+")


class ApiError(RuntimeError):
    """The server answered but the command failed (NO/BAD status, or an unusable
    response). Carries the command and the server detail, never the password."""

    def __init__(self, command: str, error: str):
        self.command = command
        self.error = error
        super().__init__(f"{command} failed: {error}")


class ReadOnlyViolation(RuntimeError):
    """A non-read command reached the transport. This adapter is read-only BY DESIGN
    (ENH-26 lands ingestion only; no send authorization exists for this platform);
    the correct fix is never to widen this list quietly — send lives behind
    core/outbox in a future, separately-authorized adapter."""

    def __init__(self, command: str):
        super().__init__(
            f"{command} is not a read command — this adapter is read-only and refuses "
            f"anything outside its allowlist before any I/O happens")


def _msgids(header_value) -> list:
    """Every <...> token in a header, in written order (References: oldest first)."""
    return _MSGID.findall(str(header_value)) if header_value else []


def _attachments(msg) -> list:
    """MIME attachment parts as content descriptors (ENH-4: attachments ARE content —
    an image-only message must not become an empty row). `url` is always None: IMAP
    has no URLs, and minting one would persist a lie; the uid in `raw` is the
    retrieval handle for a consumer authorized to fetch."""
    out = []
    for part in msg.iter_attachments():
        out.append({
            "kind": "image" if part.get_content_maintype() == "image" else "file",
            "name": part.get_filename(),
            "mimetype": part.get_content_type(),
            "url": None,
        })
    return out


class Adapter:
    def __init__(self, auth=None, imap=None):
        auth = auth or {}
        for key in ("host", "username", "password"):
            if not auth.get(key):
                raise ValueError(f"email adapter: auth['{key}'] is missing — "
                                 "configure it as an env:NAME reference in "
                                 "settings.json")
        self._host = auth["host"]
        self._username = auth["username"]
        self._password = auth["password"]
        raw_channels = auth.get("channels") or ""
        self.channels = tuple(c.strip() for c in raw_channels.split(",") if c.strip())
        if not self.channels:
            raise ValueError("email adapter: auth['channels'] is missing or empty — "
                             "a comma-separated mailbox list (e.g. \"INBOX\"), or "
                             "poll() would silently watch nothing")
        # Injectable for tests; the default speaks IMAP-over-TLS (implicit, port 993).
        self.imap = imap or (lambda host: imaplib.IMAP4_SSL(host))

    # ---- contract surface --------------------------------------------------
    def capabilities(self):
        # history is True and it matters: IMAP re-reads freely, so this platform CAN
        # supply the parity snapshot (the anti-Telegram). send is False and stays
        # False — no send authorization exists for this platform.
        return {"read": True, "history": True, "search": False,
                "send": False, "react": False, "threads": True}

    def poll(self, cursor):
        """Gap-free and idempotent: same cursor -> same messages again, never fewer.

        All-or-nothing per call (channels/slack's rule): any failure mid-read raises
        before a cursor is minted, so the engine re-polls the same window —
        duplicates are absorbed by the Message-ID-keyed store (R9), losses are not
        absorbable by anything.
        """
        offsets = self._parse_cursor(cursor)
        client = self._connect()
        try:
            messages, new_offsets = [], dict(offsets)
            for mailbox in self.channels:
                fetched, mark = self._mailbox_news(client, mailbox,
                                                   offsets.get(mailbox))
                messages.extend(fetched)
                new_offsets[mailbox] = mark
        finally:
            self._exec(client, "logout")
        if new_offsets == offsets:
            return messages, cursor
        return messages, json.dumps(new_offsets, sort_keys=True)

    def resolve(self, ref):
        """Human ref <-> platform id, both directions (channels/CONTRACT.md). Email
        ids ARE human refs: an address resolves to itself, and a mailbox name
        resolves case-insensitively to its configured canonical form (IMAP treats
        INBOX case-insensitively; other names vary by server, so only the configured
        set is answered rather than guessed)."""
        for mailbox in self.channels:
            if mailbox.lower() == ref.lower():
                return mailbox
        if "@" in ref:
            return ref
        raise LookupError(f"email adapter: cannot resolve {ref!r} — not a watched "
                          f"mailbox {list(self.channels)} and not an address")

    def health(self):
        """Cheap liveness: connect + LOGIN + LOGOUT. CAN fail (contract rule 5), and
        never selects a mailbox — liveness must not grow into a read path."""
        try:
            client = self._connect()
        except imaplib.IMAP4.error as ex:
            return {"reachable": True, "auth_ok": False,
                    "detail": f"login refused: {ex}"}
        except OSError as ex:
            return {"reachable": False, "auth_ok": False,
                    "detail": f"unreachable: {ex}"}
        try:
            self._exec(client, "noop")
        finally:
            self._exec(client, "logout")
        return {"reachable": True, "auth_ok": True,
                "detail": f"imap login ok as {self._username}"}

    # ---- optional capability: the platform snapshot (channels/CONTRACT.md) ------
    def retrievable_ts(self, channel, oldest=None, latest=None):
        """Every identity the mailbox will serve RIGHT NOW — parity's third opinion.

        Derivation is the SAME code path as poll()'s (full BODY.PEEK fetch, then
        `_identity`) on purpose: a header-only shortcut could not hash the raw bytes
        a missing-Message-ID surrogate needs, and two derivations that disagree make
        parity classify one row as missing AND extra in the same run.

        Bounds are refused, not approximated: Message-IDs do not form a range, and a
        truncated snapshot marks still-served messages "deleted upstream" — the one
        direction that hides a real loss (the contract says raise, never
        under-report).
        """
        if oldest is not None or latest is not None:
            raise ValueError(
                "email adapter: Message-ID identities do not form a range — a "
                "bounded snapshot cannot be answered exactly, and truncating one "
                "would relabel real losses as deletions; take the full snapshot")
        client = self._connect()
        try:
            self._select(client, channel)
            out = set()
            for uid in self._search_uids(client, 0):
                out.add(self._identity(self._fetch_raw(client, uid)))
            return out
        finally:
            self._exec(client, "logout")

    # ---- transport ----------------------------------------------------------
    def _exec(self, client, command, *args, **kwargs):
        """The one funnel every command passes through — deny-by-default, with the
        two IMAP writes-inside-reads forced shut here rather than avoided by
        convention at each call site."""
        if command not in READ_COMMANDS:
            raise ReadOnlyViolation(command)
        if command == "uid":
            sub = str(args[0]).upper() if args else ""
            if sub not in UID_READ_SUBCOMMANDS:
                raise ReadOnlyViolation(f"uid {sub}")
            if sub == "FETCH" and "BODY.PEEK[" not in str(args[-1]).upper():
                raise ReadOnlyViolation(
                    f"uid FETCH {args[-1]} — a fetch without BODY.PEEK sets \\Seen "
                    "on the server, a write wearing a read's name")
        if command == "select":
            kwargs["readonly"] = True
        typ, data = getattr(client, command)(*args, **kwargs)
        if typ not in ("OK", "BYE"):     # logout answers BYE by protocol
            raise ApiError(command, f"{typ}: {data!r}")
        return data

    def _connect(self):
        client = self.imap(self._host)          # OSError family when unreachable
        self._exec(client, "login", self._username, self._password)
        return client

    # ---- polling internals ---------------------------------------------------
    @staticmethod
    def _parse_cursor(cursor):
        if not cursor:
            return {}
        try:
            offsets = json.loads(cursor)
        except ValueError:
            offsets = None
        if (not isinstance(offsets, dict)
                or not all(isinstance(v, str) and _MARK.fullmatch(v)
                           for v in offsets.values())):
            raise ValueError(
                f"email adapter: cursor is not a mailbox->'uidvalidity:uid' object: "
                f"{cursor!r} — refusing to guess a polling window from corrupt state")
        return offsets

    def _mailbox_news(self, client, mailbox, mark):
        self._select(client, mailbox)
        validity = self._uidvalidity(client, mailbox)
        last = 0
        if mark is not None:
            seen_validity, seen_uid = (int(x) for x in mark.split(":"))
            if seen_validity == validity:
                last = seen_uid
            # else: the server reset UIDVALIDITY — every UID was reassigned, and
            # trusting the stale number would silently skip everything below it.
            # Restart from 0: re-served history lands as duplicates the
            # Message-ID-keyed store absorbs (never a loss).
        uids = self._search_uids(client, last)
        out = [self._normalize(mailbox, uid, validity, self._fetch_raw(client, uid))
               for uid in uids]
        return out, f"{validity}:{uids[-1] if uids else last}"

    def _select(self, client, mailbox):
        data = self._exec(client, "select", mailbox)
        if data and data[0] is None:
            raise ApiError("select", f"mailbox {mailbox!r} not selectable")

    def _uidvalidity(self, client, mailbox):
        # response() reads the untagged data the select just buffered — not a wire
        # command, so it bypasses the funnel legitimately.
        _, data = client.response("UIDVALIDITY")
        if not data or data[0] is None:
            raise ApiError("select", f"server sent no UIDVALIDITY for {mailbox!r} — "
                           "without it a cursor cannot tell a reset from a resume")
        return int(data[0])

    def _search_uids(self, client, last):
        """UIDs newer than `last`, ascending — THE ordering rule. Mailbox arrival
        order (RFC 3501: strictly ascending UIDs), never the sender-controlled Date,
        never a parse of the Message-ID. The filter back to > last absorbs the n:*
        quirk (the newest message is returned even when the range starts past it)."""
        data = self._exec(client, "uid", "SEARCH", None, "UID", f"{last + 1}:*")
        found = (data[0] or b"").split()
        return sorted(uid for uid in map(int, found) if uid > last)

    def _fetch_raw(self, client, uid):
        data = self._exec(client, "uid", "FETCH", str(uid), "(BODY.PEEK[])")
        for item in data:
            if isinstance(item, tuple) and len(item) >= 2:
                return item[1]
        # Expunged between SEARCH and FETCH: abort the whole poll (all-or-nothing);
        # the engine re-polls the same cursor and the next SEARCH won't list it.
        raise ApiError("uid FETCH", f"uid {uid} vanished mid-poll — re-poll rather "
                       "than mint a cursor past a message never ingested")

    @staticmethod
    def _identity(raw_bytes):
        """The Message-ID, canonical bracketed form — or, when the header is absent,
        a surrogate hashed from the raw bytes so every sighting of the same message
        derives the same identity (re-poll idempotency, R9). The .invalid TLD keeps
        surrogates out of the real Message-ID namespace."""
        msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
        mids = _msgids(msg.get("Message-ID"))
        if mids:
            return mids[0]
        return f"<{hashlib.sha256(raw_bytes).hexdigest()}@no-message-id.invalid>"

    def _normalize(self, mailbox, uid, validity, raw_bytes):
        msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
        mid = self._identity(raw_bytes)
        # References lists ancestors oldest-first (RFC 5322 3.6.4): its first id is
        # the thread ROOT, giving every reply the root's identity — Slack's
        # thread_ts alignment. In-Reply-To (immediate parent) is the fallback.
        chain = _msgids(msg.get("References")) or _msgids(msg.get("In-Reply-To"))
        thread = chain[0] if chain else None
        sender_name, sender_addr = email.utils.parseaddr(str(msg.get("From") or ""))
        body = msg.get_body(preferencelist=("plain", "html"))
        return {
            "channel_type": "email",
            "channel_id": mailbox,
            # The store REQUIRES a sender (R5): a From-less message (bounces,
            # calendar machinery) keeps its row behind a placeholder.
            "sender_id": sender_addr or "unknown",
            "sender_name": sender_name or None,
            "ts": mid,
            # text/plain preferred; an HTML-only message keeps its markup (the
            # contract preserves platform markup — an empty text row would classify
            # as an empty STATEMENT and be forgotten).
            "text": body.get_content() if body is not None else "",
            "thread_id": thread if thread != mid else None,
            "attachments": _attachments(msg),
            # The retrieval handle next to the content: identity alone cannot
            # re-fetch a message, (mailbox, uidvalidity, uid) can. Headers carry the
            # audit trail (Received chain, the raw References) without duplicating
            # body bytes the row already holds.
            "raw": json.dumps({"mailbox": mailbox, "uid": uid,
                               "uidvalidity": validity,
                               "headers": [[k, str(v)] for k, v in msg.items()]},
                              sort_keys=True),
        }
