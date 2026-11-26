# channels/email — the read-only IMAP adapter (ENH-26)

PHASE 5's platform: Outlook, or any RFC-3501 IMAP server. Landed as a pure dir-drop
(R11: zero `core/` discovery changes). Its purpose beyond its own utility: email is the
platform that most breaks the engine's founding assumptions, and this adapter is where
each broken assumption gets an explicit rule instead of a silent workaround.

## The platform finding

Slack and Telegram both hand core an identity that is also an ordering key (a float ts,
a monotonic message_id). Email refuses: **message identity is a Message-ID string** no
`float()` will ever parse, ordering belongs to the mailbox rather than the message, and
threading rides headers. Properties pinned by `tests/test_email_adapter.py`:

| Broken assumption | The explicit rule here |
|---|---|
| `ts` is orderable | `ts` is the **Message-ID** (canonical bracketed form). `core/parity.py` classifies these **non-orderable identities** by served-set membership; its timeline window classes are declared unavailable, never guessed (sorting an unparseable id as 0.0 would hand a real loss a benign class). |
| A message's content carries a trustworthy order | **Ordering is mailbox UID order** — arrival order, RFC 3501 strictly ascending — never the sender-controlled Date header, never a parse of the id. |
| Identity survives at the platform | A **UIDVALIDITY reset** reassigns every UID, so the cursor is `mailbox -> "uidvalidity:uid"` and a reset restarts the read from the beginning. Re-served history lands as duplicates the Message-ID-keyed store absorbs — the direction that never loses. (This is exactly why identity must not be the UID.) |
| Every message has an id | RFC 5322 makes Message-ID a SHOULD. A message without one gets a **deterministic surrogate** hashed from its raw bytes — any other source (uuid, clock, UID) mints a new identity per sighting and breaks re-poll idempotency (R9). |
| Threading has a parent ts | `thread_id` = the **first id in References** (RFC 5322 lists ancestors oldest-first, so that is the thread ROOT — the same alignment Slack's `thread_ts` gives core); **In-Reply-To** is the fallback for clients that send only it. |
| Reads are reads | IMAP hides writes inside reads. The command funnel default-denies everything outside the read allowlist, **forces every mailbox selection read-only** (EXAMINE) and **refuses any fetch without BODY.PEEK** — a plain BODY fetch marks the operator's unread mail `\Seen`. |

Unlike Telegram, IMAP is a full history platform: `retrievable_ts` exists (parity's
platform snapshot) and derives identity through the same code path as `poll()` — a
snapshot that derived it differently would make parity classify one row as missing AND
extra in the same run. Bounded snapshots are refused: Message-IDs do not form a range,
and a truncated snapshot marks still-served messages "deleted upstream", the one
direction that hides a real loss.

## Configuration

```json
{"name": "email-watch", "adapter": "email",
 "auth": {"host": "env:IMAP_HOST", "username": "env:IMAP_USERNAME",
          "password": "env:IMAP_PASSWORD", "channels": "env:IMAP_MAILBOXES"},
 "channels": [{"id": "INBOX", "label": "inbox", "reply_policy": "never"}]}
```

`channels` in `auth` is a comma-separated mailbox list (e.g. `INBOX` or
`INBOX,Archive`); each mailbox is a channel with its own cursor, and the same names
must appear under `channels[]` — the two-place rule from `docs/QUICKSTART.md` step 7.

## Explicitly NOT here

- **No send path at any layer** — no SMTP, no APPEND; the transport default-denies
  everything outside `login` / read-only `select` / `uid SEARCH` / `uid FETCH
  (BODY.PEEK)` / `noop` / `logout`.
- **No OAuth2/XOAUTH2.** The default transport is IMAP-over-TLS with `LOGIN`. On
  Outlook/M365 that requires an app password or a tenant that still permits basic
  auth; a token dance this repo cannot test end-to-end is not shipped half-done.
- **No IMAP IDLE** — this is a polling adapter under the engine's scheduler; push is a
  latency optimization with its own parity obligations (see `channels/slack_socket`).
- **No 429 dialect** — IMAP has none; servers refuse at LOGIN or throttle the socket,
  which surfaces through `health()`, not `core/ratelimit`.
- **Live smoke is operator-owned**: it needs a real mailbox credential the operator
  designates. Nothing here has run against a live server yet, and the quickstart's
  honest-limits section says so.
