# channels/slack — the Slack adapter

**Landed (ENH-18) as a dir-drop: zero `core/` changes.** Implements
[`../CONTRACT.md`](../CONTRACT.md). Its read path came first; its send path (ENH-31,
2026-10-13) is authorized **for the aws-do-agent harness only** — the harness's bot token,
its own channels, policy `staged` (a human click), never `direct`. Both are tested
invariants (`tests/test_slack_adapter.py`, `tests/test_slack_send.py`), not promises:

- `send()` is reachable only through `core/outbox`, whose ladder (durable INTENT → send →
  read-back → COMMITTED) and pre-flip guards (marker prefix, re-read of the channel at the
  click, exact canonical read-back, 3,500-character size guard, BLOCK-tier credential scan)
  stand between any caller and `chat.postMessage`; `capabilities()["direct"]` is `false`,
  so an outbox built for a `direct` policy refuses at construction;
- the transport default-denies: any Web API method outside `READ_METHODS` plus the single
  `WRITE_METHODS` entry (`chat.postMessage`) is refused before any I/O;
- a token granted read-only (this host's, 2026-08-26) still never writes: its channels'
  reply policy is `never`, and the outbox refuses a send there before anything is written.
- `COMMS_SLACK_API_BASE` points the adapter at a loopback stub for tests
  (`http://127.0.0.1:<port>` or `http://localhost:<port>` only; anything else is refused by
  name at construction) — nothing in the test suite reaches slack.com.

## What is implemented

| Method | How |
|---|---|
| `poll(cursor)` | `conversations.history`, gap-free (all pages, or fail loudly), oldest-first. The cursor is a JSON object of channel id → newest ingested ts, because `poll()` is adapter-wide while Slack history is per-channel. |
| `resolve(ref)` | id → name via `conversations.info` / `users.info`; name → id by paging `conversations.list` / `users.list`. Unresolvable refs raise. |
| `health()` | `auth.test`. Can FAIL (bad token → `auth_ok: false`; network down → `reachable: false`) and reports any active 429 hold in `detail`. |
| `send(channel_id, text, key, thread_id?)` | one `chat.postMessage`, the outbox key in the message **metadata** (`event_type: comms_engine_reply`) so `read_back` can find it without altering the approved text; `thread_ts` places a thread reply. No guard lives here — the outbox refuses before the INTENT flip. |
| `read_back(channel_id, key, thread_id?)` | for a message this process posted: fetched by its ts (history with `inclusive`, or the thread via `conversations.replies`), key AND canonical text must match (Slack escapes `&`, `<`, `>` and wraps URLs); after a restart: the newest page of history, or the thread, searched for the key. |
| `newest(channel_id, thread_id?)` | the newest top-level message, or the newest reply of a thread — the outbox's pre-send re-read. |
| `capabilities()` | `{read: true, history: true, search: false, send: true, direct: false, react: false, threads: true}` — honest, not aspirational. |

Rate limits follow contract rule 3: a 429 raises `core.ratelimit.RateLimited` with the
platform's exact `Retry-After` and the method it hit; the engine's `(instance, method)`
back-off (ENH-1) owns the waiting. The adapter never sleeps.

## Auth

Both values are `env:NAME` references in `settings.json` (see `settings.example.json`):

- `token` — the workspace token. Never a literal, anywhere.
- `channels` — comma-separated channel ids to poll. These ride the environment because
  real Slack ids are host-specific literals the sanitize gate refuses in committed
  files, and the incumbent's env contract proved the pattern in production.

## Deliberately absent

- **Any write but `chat.postMessage`** — `chat.update`, `chat.delete`, reactions, uploads and
  joins stay refused by the transport allowlist; the grant is "post a human-approved reply",
  nothing else. `direct` sends are declared off, so the engine never posts without a click.
- `search.messages`, reactions, thread traversal — not needed for the parity gate (G1);
  each lands only with a test and an allowlist entry in the same diff.
- **Any send outside the harness grant** — this host's token is read-only by
  authorization (2026-08-26) and its channels' reply policy is `never`; the send path
  exists for the aws-do-agent harness's bot token, at `staged`, and nowhere else.
- The optional loopback bridge and MCP surface from the phase-0 design sketch — out of
  scope for this adapter.
